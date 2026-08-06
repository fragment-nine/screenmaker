"""XML sidecar for a RasterMakker generation run.

Every run writes one `00_Raster_Data.xml` at the output root, next to the
generated block folders. It carries the engineering data behind the PNGs -
pixel dimensions, physical dimensions in mm/m/in, tile product and pitch, the
enabled-tile map, and the raw eng sheet row preserved verbatim - so downstream
tools (After Effects, disguise, Notch, Vectorworks) can pull a run in without
anyone going back to the spreadsheet by hand.

Deliberately stdlib-only: adding a dependency here would mean touching
ScreenMaker.spec, requirements.txt and the frozen build.
"""

import logging
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

from screens import (
    PHYSICAL_UNKNOWN,
    ENG_SCREEN_M_HEIGHT,
    ENG_SCREEN_M_WIDTH,
    parse_number,
    resolve_physical_from_repo,
)

logger = logging.getLogger(__name__)

SIDECAR_FILENAME = '00_Raster_Data.xml'
SIDECAR_VERSION = '1'

MM_PER_INCH = 25.4

# Block folders written by ScreenDrawer.draw_content / draw_eng / draw_stealth.
OUTPUT_ROLES = (
    ('content', '01_Content_Blocks'),
    ('eng', '02_Eng_Blocks'),
    ('stealth', '03_Stealth_Blocks'),
)


def _fmt(value, places=4):
    """Format a number for an XML attribute; None becomes an empty string."""
    if value is None:
        return ''
    if isinstance(value, int):
        return str(value)
    try:
        rounded = round(float(value), places)
    except (TypeError, ValueError):
        return ''
    if rounded == int(rounded):
        return str(int(rounded))
    return f'{rounded:g}'


def _screens_of(screen_list_or_screens):
    """Accept either a ScreenList or a plain list of Screen."""
    screens = getattr(screen_list_or_screens, 'screens', screen_list_or_screens)
    return list(screens or [])


def _output_filename(screen):
    """The PNG filename ScreenDrawer actually writes for this screen.

    Mirrors the format string in ScreenDrawer.draw_* exactly. Note those methods
    save under the screen's raw name, not the sanitized one - if that ever
    changes, this has to change with it or the sidecar points at files that
    aren't there.
    """
    return "{:03d}_{}.png".format(screen.num, screen.name)


def _add_physical(parent, screen):
    """<physical> - screen size recomputed from tile size x tile count."""
    width_mm, height_mm = screen.physical_size_mm()

    element = ET.SubElement(parent, 'physical')
    element.set('source', screen.physical_source or PHYSICAL_UNKNOWN)
    element.set('width_mm', _fmt(width_mm))
    element.set('height_mm', _fmt(height_mm))
    element.set('width_m', _fmt(width_mm / 1000 if width_mm else None))
    element.set('height_m', _fmt(height_mm / 1000 if height_mm else None))
    element.set('width_in', _fmt(width_mm / MM_PER_INCH if width_mm else None, 2))
    element.set('height_in', _fmt(height_mm / MM_PER_INCH if height_mm else None, 2))

    # The sheet carries its own screen dimensions. They go stale as soon as
    # anyone edits tile counts in the app, so we report ours and flag the
    # disagreement rather than silently picking a side.
    sheet_w = parse_number(screen.source_row.get(ENG_SCREEN_M_WIDTH))
    sheet_h = parse_number(screen.source_row.get(ENG_SCREEN_M_HEIGHT))
    if sheet_w is not None:
        element.set('sheet_width_m', _fmt(sheet_w))
    if sheet_h is not None:
        element.set('sheet_height_m', _fmt(sheet_h))

    mismatch = False
    for ours_mm, theirs_m in ((width_mm, sheet_w), (height_mm, sheet_h)):
        if ours_mm is None or theirs_m is None:
            continue
        ours_m = ours_mm / 1000
        if abs(ours_m - theirs_m) > max(0.01, abs(theirs_m) * 0.005):
            mismatch = True
    if mismatch:
        element.set('sheet_mismatch', 'true')

    return element


def _add_screen(parent, screen, index):
    element = ET.SubElement(parent, 'screen')
    element.set('index', str(index))
    element.set('name', str(screen.name))
    if screen.product:
        element.set('product', str(screen.product))

    pixels = ET.SubElement(element, 'pixels')
    pixels.set('width', _fmt(screen.width))
    pixels.set('height', _fmt(screen.height))

    total, enabled, disabled = screen.tile_counts()
    tiles = ET.SubElement(element, 'tiles')
    tiles.set('wide', _fmt(screen.tiles_w))
    tiles.set('high', _fmt(screen.tiles_h))
    tiles.set('total', str(total))
    tiles.set('enabled', str(enabled))
    tiles.set('disabled', str(disabled))
    tiles.set('tile_px_width', _fmt(screen.tile_width))
    tiles.set('tile_px_height', _fmt(screen.tile_height))
    tiles.set('tile_mm_width', _fmt(screen.tile_mm_width))
    tiles.set('tile_mm_height', _fmt(screen.tile_mm_height))
    tiles.set('pitch_mm', _fmt(screen.pitch_mm))

    _add_physical(element, screen)

    ET.SubElement(element, 'enabled_map').text = screen.enabled_map()

    outputs = ET.SubElement(element, 'outputs')
    filename = _output_filename(screen)
    for role, folder in OUTPUT_ROLES:
        file_el = ET.SubElement(outputs, 'file')
        file_el.set('role', role)
        file_el.set('path', f'{folder}/{filename}')

    # The eng sheet row, untouched. Column names live in an attribute rather than
    # becoming tag names because the real headers contain spaces, '%' and '#'.
    if screen.source_row:
        source_row = ET.SubElement(element, 'source_row')
        for key, value in screen.source_row.items():
            if key is None:
                continue
            name = str(key).strip()
            if not name:
                continue
            field = ET.SubElement(source_row, 'field')
            field.set('name', name)
            field.text = '' if value is None else str(value)

    return element


def build_tree(screen_list_or_screens, csv_path=None, generated=None, tiles=None):
    """Build the sidecar ElementTree without writing it (used by tests).

    `tiles` overrides the LED tile repository lookup; pass a list to inject
    records, or an empty list to skip the database entirely.
    """
    screens = _screens_of(screen_list_or_screens)

    # Fill physical dimensions from the tile repository for any screen the eng
    # sheet gave us nothing for. Fetched once, not once per screen.
    if tiles is None and any(not (s.tile_mm_width and s.tile_mm_height) for s in screens):
        try:
            from database import DatabaseManager
            tiles = DatabaseManager().get_all_tiles()
        except Exception as e:
            logger.warning("Tile repository unavailable, skipping lookup: %s", e)
            tiles = []
    if tiles is not None:
        for screen in screens:
            resolve_physical_from_repo(screen, tiles)

    root = ET.Element('rastermakker')
    root.set('version', SIDECAR_VERSION)
    root.set('generated', (generated or datetime.now().astimezone()).isoformat(timespec='seconds'))
    if csv_path:
        root.set('source_csv', Path(csv_path).name)

    screens_el = ET.SubElement(root, 'screens')
    screens_el.set('count', str(len(screens)))
    for index, screen in enumerate(screens):
        _add_screen(screens_el, screen, index)

    return ET.ElementTree(root)


def write_sidecar(screen_list_or_screens, output_path, csv_path=None, tiles=None):
    """Write the run's XML sidecar and return the path it was written to."""
    output_path = Path(output_path)
    output_path.mkdir(parents=True, exist_ok=True)
    destination = output_path / SIDECAR_FILENAME

    tree = build_tree(screen_list_or_screens, csv_path, tiles=tiles)
    ET.indent(tree, space='  ')
    tree.write(destination, encoding='utf-8', xml_declaration=True)

    return destination
