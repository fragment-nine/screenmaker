import sys
import tempfile
from pathlib import Path
from xml.etree import ElementTree as ET

from screens import (
    PHYSICAL_FROM_REPO,
    PHYSICAL_FROM_SHEET,
    PHYSICAL_UNKNOWN,
    Screen,
    ScreenList,
    parse_number,
    resolve_physical_from_repo,
)
from sidecar import SIDECAR_FILENAME, build_tree, write_sidecar

ROOT = Path(__file__).resolve().parent.parent
TEMP_CSV_DIR = ROOT / 'temp'


def _root_for(screen_list, csv_path=None, tiles=None):
    # tiles=[] keeps the tests off the user's real LED tile database.
    return build_tree(screen_list, csv_path, tiles=tiles if tiles is not None else []).getroot()


def test_parse_number():
    print("Testing parse_number...")
    # The sheets are full of Excel error strings and blanks; none may raise.
    assert parse_number('#DIV/0!') is None
    assert parse_number('#REF!') is None
    assert parse_number('') is None
    assert parse_number('   ') is None
    assert parse_number(None) is None
    assert parse_number('N/A') is None
    assert parse_number('600') == 600.0
    assert parse_number('5.77') == 5.77
    assert parse_number('1,234.5') == 1234.5
    assert parse_number('1200mm') == 1200.0
    assert parse_number(208) == 208.0
    print("✓ parse_number tolerates junk cells")


def test_eng_sheet_round_trip():
    print("Testing eng sheet -> sidecar round trip (LP.csv)...")
    screen_list = ScreenList(TEMP_CSV_DIR / 'LP.csv')
    assert screen_list.screens, "LP.csv should parse into screens"

    root = _root_for(screen_list, TEMP_CSV_DIR / 'LP.csv')
    assert root.tag == 'rastermakker'
    assert root.get('source_csv') == 'LP.csv'

    screens_el = root.find('screens')
    assert int(screens_el.get('count')) == len(screen_list.screens)

    # 'US Wall' in LP.csv: 37 x 11 tiles of 600 x 1200 mm, 104 x 208 px.
    us_wall = next(
        s for s in screens_el.findall('screen')
        if s.get('name').strip() == 'US Wall'
    )
    assert us_wall.get('product') == 'CB5'

    pixels = us_wall.find('pixels')
    assert pixels.get('width') == '3848'
    assert pixels.get('height') == '2288'

    tiles = us_wall.find('tiles')
    assert tiles.get('wide') == '37'
    assert tiles.get('high') == '11'
    assert tiles.get('total') == '407'
    assert tiles.get('enabled') == '407'
    assert tiles.get('disabled') == '0'
    assert tiles.get('tile_mm_width') == '600'
    assert tiles.get('tile_mm_height') == '1200'
    assert tiles.get('pitch_mm') == '5.77'

    physical = us_wall.find('physical')
    assert physical.get('source') == PHYSICAL_FROM_SHEET
    assert physical.get('width_mm') == '22200'
    assert physical.get('height_mm') == '13200'
    assert physical.get('width_m') == '22.2'
    assert physical.get('height_m') == '13.2'
    # 22200 mm / 25.4
    assert physical.get('width_in') == '874.02'
    # Our recomputed size agrees with the sheet's own Screen M Width/Height.
    assert physical.get('sheet_width_m') == '22.2'
    assert physical.get('sheet_mismatch') is None
    print("✓ physical dimensions derived and agree with the sheet")

    # Every column of the sheet row survives verbatim.
    fields = {f.get('name'): f.text for f in us_wall.find('source_row')}
    assert fields['Screen M Width'] == '22.2'
    assert fields['Screen M Height'] == '13.2'
    assert fields['AE SCALE FACTOR W'] == '0.498960499'
    # Column names are stripped (the sheet has trailing spaces in its headers),
    # values are not.
    assert fields['Total Pixels'] == '8804224'
    assert fields['% of a 4K Raster'] == '1.061466049'
    print(f"✓ source_row preserved verbatim ({len(fields)} columns)")

    # Output paths match what ScreenDrawer actually writes, including the
    # trailing space the sheet has in this screen's name.
    source = next(s for s in screen_list.screens if s.name.strip() == 'US Wall')
    paths = {f.get('role'): f.get('path') for f in us_wall.find('outputs')}
    expected = "{:03d}_{}.png".format(source.num, source.name)
    assert paths['content'] == f'01_Content_Blocks/{expected}'
    assert paths['eng'] == f'02_Eng_Blocks/{expected}'
    assert paths['stealth'] == f'03_Stealth_Blocks/{expected}'
    print(f"✓ output paths match the generated PNG filenames ({expected})")


def test_second_sheet_parses():
    print("Testing EOTS.csv...")
    screen_list = ScreenList(TEMP_CSV_DIR / 'EOTS.csv')
    assert screen_list.screens, "EOTS.csv should parse into screens"
    root = _root_for(screen_list, TEMP_CSV_DIR / 'EOTS.csv')

    upstage = root.find('.//screen')
    physical = upstage.find('physical')
    # 20 x 4 tiles of 600 x 1200 mm -> 12.0 m x 4.8 m, matching the sheet.
    assert physical.get('width_m') == '12'
    assert physical.get('height_m') == '4.8'
    assert physical.get('sheet_mismatch') is None
    print("✓ EOTS.csv derives correctly and raises nothing")


def test_disabled_tiles():
    print("Testing disabled tile accounting...")
    screen = Screen('Wall', 100, 100, 4, 3, num=0)
    screen.enabled_array[0][0] = False
    screen.enabled_array[2][3] = False

    root = _root_for([screen])
    tiles = root.find('.//tiles')
    assert tiles.get('total') == '12'
    assert tiles.get('enabled') == '10'
    assert tiles.get('disabled') == '2'

    # Same ';'-delimited serialization save_to_csv writes.
    assert root.find('.//enabled_map').text == '0111;1111;1110'
    print("✓ enabled/disabled counts and enabled_map correct")


def test_missing_physical_data():
    print("Testing screens with no physical data...")
    # A screen the sheet gave nothing for, and whose pixel size matches no tile.
    screen = Screen('Mystery', 137, 137, 2, 2, num=0)
    root = _root_for([screen])

    physical = root.find('.//physical')
    assert physical.get('source') == PHYSICAL_UNKNOWN
    # Empty attributes rather than garbage or a crash.
    assert physical.get('width_mm') == ''
    assert physical.get('height_m') == ''
    assert physical.get('sheet_mismatch') is None

    tiles = root.find('.//tiles')
    assert tiles.get('tile_mm_width') == ''
    # Pixel data is still there and correct.
    assert root.find('.//pixels').get('width') == '274'
    print("✓ missing physical data degrades to empty attributes")


def test_junk_cells_do_not_raise():
    print("Testing junk eng sheet cells...")
    screen = Screen(
        'Junk', 104, 208, 4, 2, num=0,
        source_row={
            'WALL': 'Junk',
            'Screen M Width': '#DIV/0!',
            'Screen M Height': '',
            'Pitch (mm)': '#REF!',
            'Notes': 'has, a comma & an <angle> bracket',
        },
    )
    root = _root_for([screen])
    physical = root.find('.//physical')
    # Unparseable sheet dimensions are simply absent, not flagged as a mismatch.
    assert physical.get('sheet_width_m') is None
    assert physical.get('sheet_mismatch') is None

    fields = {f.get('name'): f.text for f in root.find('.//source_row')}
    assert fields['Screen M Width'] == '#DIV/0!'
    assert fields['Notes'] == 'has, a comma & an <angle> bracket'
    print("✓ junk cells pass through without raising or corrupting the XML")


def test_illegal_xml_characters():
    print("Testing illegal XML characters in sheet cells...")
    # A stray control character in one notes field must not cost the whole run
    # its sidecar - ElementTree refuses to serialize a document containing one.
    screen = Screen(
        'Bell\x07Wall', 104, 208, 2, 2, num=0,
        source_row={'WALL': 'Bell\x07Wall', 'Notes': 'vertical\x0btab', 'Naming': 'ok'},
    )
    root = _root_for([screen])
    assert root.find('.//screen').get('name') == 'BellWall'

    fields = {f.get('name'): f.text for f in root.find('.//source_row')}
    assert fields['Notes'] == 'verticaltab'
    assert fields['Naming'] == 'ok'

    # The output paths embed the screen name, so they need stripping too.
    for f in root.find('.//outputs'):
        assert '\x07' not in f.get('path'), f.get('path')

    # And it still serializes and reparses cleanly.
    assert ET.fromstring(ET.tostring(root)) is not None
    print("✓ control characters stripped, document still serializes")


def test_repo_fallback():
    print("Testing tile repository fallback...")
    tiles = [{
        'name': 'Absen Polaris 2.5',
        'brand': 'Absen',
        'pixel_width': 200,
        'pixel_height': 200,
        'physical_width': 500.0,
        'physical_height': 500.0,
        'pitch': 2.5,
    }]

    screen = Screen('From Repo', 200, 200, 6, 4, num=0)
    resolve_physical_from_repo(screen, tiles)
    assert screen.physical_source == PHYSICAL_FROM_REPO
    assert screen.tile_mm_width == 500.0
    assert screen.pitch_mm == 2.5

    root = _root_for([screen], tiles=tiles)
    physical = root.find('.//physical')
    assert physical.get('source') == PHYSICAL_FROM_REPO
    assert physical.get('width_m') == '3'
    assert physical.get('height_m') == '2'

    # A screen the sheet already covered is never overwritten by the repo.
    from_sheet = Screen(
        'From Sheet', 200, 200, 6, 4, num=0,
        tile_mm_width=600.0, tile_mm_height=600.0,
        physical_source=PHYSICAL_FROM_SHEET,
    )
    resolve_physical_from_repo(from_sheet, tiles)
    assert from_sheet.tile_mm_width == 600.0
    assert from_sheet.physical_source == PHYSICAL_FROM_SHEET
    print("✓ repo fallback fills gaps without overriding the sheet")


def test_ambiguous_repo_match_is_skipped():
    print("Testing ambiguous tile repository match...")
    tiles = [
        {'name': 'Brand A P2', 'brand': 'A', 'pixel_width': 176, 'pixel_height': 176,
         'physical_width': 500.0, 'physical_height': 500.0, 'pitch': 2.8},
        {'name': 'Brand B P3', 'brand': 'B', 'pixel_width': 176, 'pixel_height': 176,
         'physical_width': 600.0, 'physical_height': 600.0, 'pitch': 3.4},
    ]
    screen = Screen('Ambiguous', 176, 176, 3, 3, num=0)
    resolve_physical_from_repo(screen, tiles)
    # Two tiles share the pixel size and nothing disambiguates them: reporting
    # unknown beats guessing a physical size that ends up in a drawing.
    assert screen.tile_mm_width is None
    assert screen.physical_source == PHYSICAL_UNKNOWN
    print("✓ ambiguous pixel-size matches are left unknown, not guessed")


def test_sheet_mismatch_flagged():
    print("Testing stale sheet dimension detection...")
    # Sheet says 12 m wide, but the tile count was edited down in the app.
    screen = Screen(
        'Edited', 104, 208, 10, 4, num=0,
        tile_mm_width=600.0, tile_mm_height=1200.0,
        physical_source=PHYSICAL_FROM_SHEET,
        source_row={'Screen M Width': '12', 'Screen M Height': '4.8'},
    )
    physical = _root_for([screen]).find('.//physical')
    assert physical.get('width_m') == '6'          # ours, recomputed
    assert physical.get('sheet_width_m') == '12'   # theirs, preserved
    assert physical.get('sheet_mismatch') == 'true'
    print("✓ stale sheet dimensions are surfaced, not silently resolved")


def test_write_sidecar_to_disk():
    print("Testing write_sidecar...")
    screen_list = ScreenList(TEMP_CSV_DIR / 'LP.csv')
    with tempfile.TemporaryDirectory() as tmp:
        destination = write_sidecar(screen_list, tmp, TEMP_CSV_DIR / 'LP.csv', tiles=[])
        assert destination.name == SIDECAR_FILENAME
        assert destination.exists()

        text = destination.read_text(encoding='utf-8')
        assert text.startswith("<?xml version='1.0' encoding='utf-8'?>")
        assert '\n  <screens' in text, "output should be indented for humans"

        # Reparse from disk the way a downstream tool would.
        root = ET.parse(destination).getroot()
        names = [s.get('name').strip() for s in root.findall('.//screen')]
        assert 'US Wall' in names
        print(f"✓ wrote and reparsed {destination.name} ({len(names)} screens)")


if __name__ == "__main__":
    tests = [
        test_parse_number,
        test_eng_sheet_round_trip,
        test_second_sheet_parses,
        test_disabled_tiles,
        test_missing_physical_data,
        test_junk_cells_do_not_raise,
        test_illegal_xml_characters,
        test_repo_fallback,
        test_ambiguous_repo_match_is_skipped,
        test_sheet_mismatch_flagged,
        test_write_sidecar_to_disk,
    ]
    try:
        for test in tests:
            test()
        print("ALL TESTS PASSED")
    except Exception as e:
        print(f"TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
