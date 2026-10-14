"""ScreenMaker Flask web application."""

import sys
import os
import uuid
import csv
import copy
import math
import zipfile
import tempfile
import threading
import shutil
import io
from pathlib import Path
from functools import wraps

# Make src/ importable
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from flask import (
    Flask, Blueprint, request, session, jsonify, send_file,
    render_template, redirect, url_for,
)
import session_store
from screens import Screen, ScreenList, ScreenDrawer, PHYSICAL_UNKNOWN
from sidecar import write_sidecar
from database import DatabaseManager

# ── basecamp integration ──────────────────────────────────────────────────
# Flag-gated per basecamp/docs/INTEGRATING-A-TOOL.md: standalone deploy
# (BASECAMP_MODE unset) behaves exactly as before. In basecamp mode the app
# runs under a /<tool> basePath behind the gateway's reverse proxy and trusts
# the gateway's signed X-Basecamp-User header instead of its own password
# login.
BASECAMP_MODE = os.environ.get('BASECAMP_MODE') == '1'
BASE_PATH = os.environ.get('BASE_PATH', '').rstrip('/') if BASECAMP_MODE else ''
BASECAMP_ORIGIN = os.environ.get('BASECAMP_ORIGIN', 'https://basecamp.f9.live').rstrip('/')

app = Flask(__name__, static_url_path=f'{BASE_PATH}/static')
app.secret_key = os.environ.get('SECRET_KEY', os.urandom(24).hex())
app.config['PERMANENT_SESSION_LIFETIME'] = 7 * 24 * 3600  # 7 days
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.jinja_env.globals.update(
    BASECAMP_MODE=BASECAMP_MODE,
    BASECAMP_ORIGIN=BASECAMP_ORIGIN,
    BASE_PATH=BASE_PATH,
)

PASSWORD = os.environ.get('SCREENMAKER_PASSWORD', 'screenmaker')


if BASECAMP_MODE:
    from urllib.parse import urlsplit

    _external = urlsplit(BASECAMP_ORIGIN)

    class _BehindBasecampGateway:
        """Make the app build URLs against the address the browser actually used.

        The gateway reverse-proxies with changeOrigin, so the Host header this
        app receives is the backend's own Cloud Run hostname, not
        basecamp.f9.live. Werkzeug builds *absolute* redirect URLs from that
        host — including the automatic trailing-slash redirect for
        /screenmaker -> /screenmaker/ — which would bounce the browser straight
        at the locked backend URL and hand it a 403. Pinning the externally
        visible scheme/host here fixes every such redirect at once.
        """

        def __init__(self, wsgi_app):
            self.wsgi_app = wsgi_app

        def __call__(self, environ, start_response):
            environ['wsgi.url_scheme'] = _external.scheme or 'https'
            environ['HTTP_HOST'] = _external.netloc
            return self.wsgi_app(environ, start_response)

    app.wsgi_app = _BehindBasecampGateway(app.wsgi_app)


bp = Blueprint('screenmaker', __name__)

# In-process job store  { job_id: {status, progress, total, zip_path, error} }
_jobs: dict = {}
_jobs_lock = threading.Lock()

session_store.init()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _basecamp_user() -> str | None:
    """Trusted identity set by the gateway. Stripped/overwritten by proxy.js
    on the way in, so this header can only carry a real, authenticated
    email — see basecamp/docs/INTEGRATING-A-TOOL.md §B."""
    return request.headers.get('X-Basecamp-User') or None


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if BASECAMP_MODE:
            if not _basecamp_user():
                # The gateway only proxies authenticated requests; a missing
                # header here means direct/unproxied access to the backend.
                return jsonify({'error': 'Unauthorized'}), 401
            return f(*args, **kwargs)
        if not session.get('authenticated'):
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({'error': 'Unauthorized'}), 401
            return redirect(url_for('.login_page'))
        return f(*args, **kwargs)
    return decorated


def screen_to_dict(screen: Screen) -> dict:
    return {
        'id': screen.num,
        'name': screen.name,
        'tile_width': screen.tile_width,
        'tile_height': screen.tile_height,
        'tiles_w': screen.tiles_w,
        'tiles_h': screen.tiles_h,
        'enabled_array': screen.enabled_array,
        'color_hue': screen.colorBGHue,
        'width_px': screen.width,
        'height_px': screen.height,
        # Eng sheet passthrough. Screens round-trip through session_store between
        # requests, so anything not carried here is lost before generation and the
        # sidecar comes out empty.
        'source_row': screen.source_row,
        'tile_mm_width': screen.tile_mm_width,
        'tile_mm_height': screen.tile_mm_height,
        'pitch_mm': screen.pitch_mm,
        'product': screen.product,
        'physical_source': screen.physical_source,
    }


def dict_to_screen(d: dict) -> Screen:
    screen = Screen(
        name=d['name'],
        tile_width=d['tile_width'],
        tile_height=d['tile_height'],
        tiles_w=d['tiles_w'],
        tiles_h=d['tiles_h'],
        num=d['id'],
        enabled_array=d['enabled_array'],
        source_row=d.get('source_row'),
        tile_mm_width=d.get('tile_mm_width'),
        tile_mm_height=d.get('tile_mm_height'),
        pitch_mm=d.get('pitch_mm'),
        product=d.get('product'),
        physical_source=d.get('physical_source', PHYSICAL_UNKNOWN),
    )
    screen.colorBGHue = d.get('color_hue', 0)
    return screen


def resize_enabled_array(old_array: list, new_rows: int, new_cols: int) -> list:
    """Grow or shrink enabled_array; new cells default to True."""
    result = []
    for r in range(new_rows):
        row = []
        for c in range(new_cols):
            if r < len(old_array) and c < len(old_array[r]):
                row.append(old_array[r][c])
            else:
                row.append(True)
        result.append(row)
    return result


def _session_key() -> str | None:
    """Key into session_store for this caller's saved screens. In basecamp
    mode there's no local login/cookie to key off, so the trusted email
    itself is the key — stable across devices and independent of whether
    Flask's own session cookie survives the gateway's proxy hop."""
    if BASECAMP_MODE:
        email = _basecamp_user()
        return f'bc:{email}' if email else None
    return session.get('session_id')


def _get_session_screens() -> list:
    key = _session_key()
    if not key:
        return []
    return session_store.get(key).get('screens', [])


def _save_session_screens(screens: list):
    key = _session_key()
    if not key:
        return
    data = session_store.get(key)
    data['screens'] = screens
    session_store.save(key, data)


def _cleanup_job(job_id: str, zip_path: str):
    try:
        os.unlink(zip_path)
    except Exception:
        pass
    with _jobs_lock:
        _jobs.pop(job_id, None)


# ── Auth routes ────────────────────────────────────────────────────────────────

@bp.route('/login', methods=['GET', 'POST'])
def login_page():
    if BASECAMP_MODE:
        # No local login in basecamp mode — the gateway's SSO is the only
        # sign-in surface, and login_required already trusts its header.
        return redirect(url_for('.index'))

    if session.get('authenticated'):
        return redirect(url_for('.index'))

    error = None
    if request.method == 'POST':
        if request.form.get('password', '') == PASSWORD:
            session.permanent = True
            session['authenticated'] = True
            if 'session_id' not in session:
                session['session_id'] = str(uuid.uuid4())
            return redirect(url_for('.index'))
        error = 'Incorrect password.'

    return render_template('login.html', error=error)


@bp.route('/logout')
def logout():
    if BASECAMP_MODE:
        # Sign-out lives in the shared chrome header's user menu.
        return redirect(url_for('.index'))
    session.clear()
    return redirect(url_for('.login_page'))


@bp.route('/')
@login_required
def index():
    return render_template('index.html')


# ── Screen API ─────────────────────────────────────────────────────────────────

def _recalculate_hues(screens: list):
    """Evenly distribute HSV hues across all screens."""
    n = len(screens)
    if n == 0:
        return
    offset = int(360 / n)
    for i, s in enumerate(screens):
        s['color_hue'] = i * offset


@bp.route('/api/screens', methods=['GET'])
@login_required
def get_screens():
    return jsonify(_get_session_screens())


@bp.route('/api/screens', methods=['POST'])
@login_required
def create_screen():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': 'name required'}), 400

    try:
        tile_width = float(data.get('tile_width', 60))
        tile_height = float(data.get('tile_height', 60))
        tiles_w = float(data.get('tiles_w', 8))
        tiles_h = float(data.get('tiles_h', 6))
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid numeric values'}), 400

    screens = _get_session_screens()
    new_id = max((s['id'] for s in screens), default=-1) + 1

    screen = Screen(
        name=name,
        tile_width=tile_width,
        tile_height=tile_height,
        tiles_w=tiles_w,
        tiles_h=tiles_h,
        num=new_id,
    )
    screen_dict = screen_to_dict(screen)
    screens.append(screen_dict)
    _recalculate_hues(screens)
    _save_session_screens(screens)

    return jsonify({'screens': screens, 'new_id': new_id}), 201


@bp.route('/api/upload-csv', methods=['POST'])
@login_required
def upload_csv():
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    f = request.files['file']
    if not f.filename.lower().endswith('.csv'):
        return jsonify({'error': 'File must be a .csv'}), 400

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode='wb', suffix='.csv', delete=False) as tmp:
            f.save(tmp)
            tmp_path = tmp.name

        screen_list = ScreenList(tmp_path)
        if not screen_list.screens:
            return jsonify({
                'error': (
                    'Could not parse CSV. '
                    'Required columns: WALL, Tile_Px_Width, Tile_Px_Height, '
                    'Tiles_Wide, Tiles_High'
                )
            }), 400

        screens_data = [screen_to_dict(s) for s in screen_list.screens]
        _save_session_screens(screens_data)
        return jsonify({'screens': screens_data})

    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


@bp.route('/api/screens/<int:screen_id>', methods=['PATCH'])
@login_required
def update_screen(screen_id):
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    screens = _get_session_screens()
    screen_dict = next((s for s in screens if s['id'] == screen_id), None)
    if screen_dict is None:
        return jsonify({'error': 'Screen not found'}), 404

    for key in ('name', 'tile_width', 'tile_height', 'tiles_w', 'tiles_h'):
        if key in data:
            screen_dict[key] = data[key]

    # Recalculate pixel dimensions
    screen_dict['width_px'] = int(screen_dict['tile_width'] * screen_dict['tiles_w'])
    screen_dict['height_px'] = int(screen_dict['tile_height'] * screen_dict['tiles_h'])

    # Resize enabled_array if tile counts changed
    new_rows = math.ceil(screen_dict['tiles_h'])
    new_cols = math.ceil(screen_dict['tiles_w'])
    old_array = screen_dict['enabled_array']
    if new_rows != len(old_array) or new_cols != (len(old_array[0]) if old_array else 0):
        screen_dict['enabled_array'] = resize_enabled_array(old_array, new_rows, new_cols)

    _save_session_screens(screens)
    return jsonify(screen_dict)


@bp.route('/api/screens/<int:screen_id>/tiles', methods=['PATCH'])
@login_required
def update_tiles(screen_id):
    data = request.get_json()
    if not data or 'enabled_array' not in data:
        return jsonify({'error': 'enabled_array required'}), 400

    screens = _get_session_screens()
    screen_dict = next((s for s in screens if s['id'] == screen_id), None)
    if screen_dict is None:
        return jsonify({'error': 'Screen not found'}), 404

    screen_dict['enabled_array'] = data['enabled_array']
    _save_session_screens(screens)
    return jsonify({'ok': True})


@bp.route('/api/screens/<int:screen_id>', methods=['DELETE'])
@login_required
def delete_screen(screen_id):
    screens = _get_session_screens()
    screen_dict = next((s for s in screens if s['id'] == screen_id), None)
    if screen_dict is None:
        return jsonify({'error': 'Screen not found'}), 404

    screens.remove(screen_dict)
    _recalculate_hues(screens)
    _save_session_screens(screens)
    return jsonify({'screens': screens})


@bp.route('/api/screens/<int:screen_id>/duplicate', methods=['POST'])
@login_required
def duplicate_screen(screen_id):
    screens = _get_session_screens()
    screen_dict = next((s for s in screens if s['id'] == screen_id), None)
    if screen_dict is None:
        return jsonify({'error': 'Screen not found'}), 404

    new_id = max((s['id'] for s in screens), default=-1) + 1
    new_screen = copy.deepcopy(screen_dict)
    new_screen['id'] = new_id
    new_screen['name'] = f"{screen_dict['name']} copy"

    screens.append(new_screen)
    _recalculate_hues(screens)
    _save_session_screens(screens)
    return jsonify({'screens': screens, 'new_id': new_id}), 201


@bp.route('/api/clear', methods=['POST'])
@login_required
def clear_all():
    key = _session_key()
    if key:
        session_store.clear_screens(key)
    return jsonify({'ok': True})


@bp.route('/api/export-csv')
@login_required
def export_csv():
    screens = _get_session_screens()
    if not screens:
        return jsonify({'error': 'No screens to export'}), 400

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['WALL', 'Tile_Px_Width', 'Tile_Px_Height', 'Tiles_Wide', 'Tiles_High', 'Enabled_Array'])
    for s in screens:
        enabled_str = ';'.join(
            ''.join(str(int(v)) for v in row)
            for row in s['enabled_array']
        )
        writer.writerow([
            s['name'], s['tile_width'], s['tile_height'],
            s['tiles_w'], s['tiles_h'], enabled_str,
        ])

    output.seek(0)
    return send_file(
        io.BytesIO(output.getvalue().encode()),
        mimetype='text/csv',
        as_attachment=True,
        download_name='screens.csv',
    )


# ── LED Tile Repository ────────────────────────────────────────────────────────

@bp.route('/api/tiles', methods=['GET'])
@login_required
def get_tiles():
    return jsonify(DatabaseManager().get_all_tiles())


@bp.route('/api/tiles', methods=['POST'])
@login_required
def add_tile():
    data = request.get_json()
    if not data or 'name' not in data:
        return jsonify({'error': 'name required'}), 400
    tile_id = DatabaseManager().add_tile(data)
    return jsonify({'id': tile_id}), 201


# ── Image Generation ───────────────────────────────────────────────────────────

def _run_generation(job_id: str, screens: list):
    """Background worker: generate PNGs for all screens and zip the results."""
    tmp_dir = None
    try:
        with _jobs_lock:
            _jobs[job_id]['status'] = 'running'

        tmp_dir = tempfile.mkdtemp(prefix='screenmaker_')
        output_path = Path(tmp_dir)
        for subdir in ('01_Content_Blocks', '02_Eng_Blocks', '03_Stealth_Blocks'):
            (output_path / subdir).mkdir()

        for i, screen in enumerate(screens):
            drawer = ScreenDrawer(screen, output_path)
            drawer.draw_content()
            drawer.draw_eng()
            drawer.draw_stealth()
            with _jobs_lock:
                _jobs[job_id]['progress'] = i + 1

        # Sidecar goes in the download too. A sidecar failure must not cost the
        # user their rendered blocks, so it is logged rather than raised.
        try:
            write_sidecar(screens, output_path)
        except Exception as e:
            app.logger.warning("Could not write XML sidecar: %s", e)

        # Build ZIP from every generated file (PNG blocks + the XML sidecar)
        zip_fd, zip_path = tempfile.mkstemp(suffix='.zip', prefix='screenmaker_out_')
        os.close(zip_fd)
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for item in sorted(output_path.rglob('*')):
                if item.is_file():
                    zf.write(item, item.relative_to(output_path))

        with _jobs_lock:
            _jobs[job_id]['status'] = 'complete'
            _jobs[job_id]['zip_path'] = zip_path

    except Exception as e:
        with _jobs_lock:
            _jobs[job_id]['status'] = 'error'
            _jobs[job_id]['error'] = str(e)
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


@bp.route('/api/generate', methods=['POST'])
@login_required
def generate():
    screens_data = _get_session_screens()
    if not screens_data:
        return jsonify({'error': 'No screens loaded'}), 400

    screens = [dict_to_screen(s) for s in screens_data]
    job_id = str(uuid.uuid4())

    with _jobs_lock:
        _jobs[job_id] = {
            'status': 'pending',
            'progress': 0,
            'total': len(screens),
            'zip_path': None,
            'error': None,
        }

    thread = threading.Thread(target=_run_generation, args=(job_id, screens), daemon=True)
    thread.start()
    return jsonify({'job_id': job_id})


@bp.route('/api/generate/<job_id>/status')
@login_required
def job_status(job_id):
    with _jobs_lock:
        job = dict(_jobs.get(job_id, {}))
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    # Don't send the local filesystem path to the client
    job.pop('zip_path', None)
    return jsonify(job)


@bp.route('/api/generate/<job_id>/download')
@login_required
def download_zip(job_id):
    with _jobs_lock:
        job = dict(_jobs.get(job_id, {}))

    if not job:
        return jsonify({'error': 'Job not found'}), 404
    if job.get('status') != 'complete':
        return jsonify({'error': 'Job not complete'}), 400

    zip_path = job.get('zip_path')
    if not zip_path or not os.path.exists(zip_path):
        return jsonify({'error': 'Output file not found'}), 404

    # Schedule cleanup 60 s after the response is sent
    timer = threading.Timer(60, _cleanup_job, args=[job_id, zip_path])
    timer.daemon = True
    timer.start()

    return send_file(zip_path, as_attachment=True, download_name='screenmaker_output.zip')


app.register_blueprint(bp, url_prefix=BASE_PATH)


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8088))
    app.run(debug=True, host='0.0.0.0', port=port)
