import os, sys, math, colorsys, csv, logging, re
from unicodedata import name 
from pathlib import Path
from PIL import Image, ImageFont, ImageDraw

gray = (50, 50, 50)
gray2 = (10,10,10)
gray3 = (130,130,130)
red =  (255, 0, 0)
blue = (0,0,255)

logger = logging.getLogger(__name__)

#Root Dir 

if getattr(sys, 'frozen', False):
    root_dir = Path(sys._MEIPASS)
else:
    root_dir = Path(__file__).resolve().parent.parent

#Set default font 
ImageDraw.ImageDraw.font = ImageFont.truetype(root_dir / 'lib'/ 'font' /'RobotoMono-Light.ttf')

# Eng sheet column names we pull typed values out of. The sheet is a hand-maintained
# spreadsheet export, so treat every one of these as optional.
ENG_TILE_MM_WIDTH = 'Tile MM Width'
ENG_TILE_MM_HEIGHT = 'Tile MM Height'
ENG_PITCH_MM = 'Pitch (mm)'
ENG_PRODUCT = 'Product'
ENG_SCREEN_M_WIDTH = 'Screen M Width'
ENG_SCREEN_M_HEIGHT = 'Screen M Height'

# Where a screen's physical dimensions came from.
PHYSICAL_FROM_SHEET = 'eng_sheet'
PHYSICAL_FROM_REPO = 'tile_repository'
PHYSICAL_UNKNOWN = 'unknown'


def parse_number(value):
    """Best-effort float from an eng sheet cell, or None.

    The sheets are full of '#DIV/0!', '#REF!', blanks, thousands separators and
    stray units. Nothing downstream of this should ever raise on a junk cell, so
    anything unparseable comes back as None rather than propagating an error.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    if not text or text.startswith('#'):
        return None

    # Drop thousands separators and anything that isn't part of a number.
    text = text.replace(',', '')
    match = re.search(r'-?\d*\.?\d+', text)
    if not match:
        return None
    try:
        return float(match.group())
    except ValueError:
        return None


class Screen: 
    def __init__(self, name, tile_width, tile_height, tiles_w, tiles_h, **kwargs) -> None:
        self.name = name
        self.width = int(tile_width * tiles_w) 
        self.height = int(tile_height * tiles_h)
        self.tile_width = tile_width
        self.tile_height = tile_height
        self.tiles_w = tiles_w
        self.tiles_h = tiles_h
        self.colorBGHue = 0
        self.num = kwargs.get('num', 0)
        self.enabled_array = kwargs.get('enabled_array', [[True for _ in range(math.ceil(self.tiles_w))] for _ in range(math.ceil(self.tiles_h))])

        # Eng sheet passthrough. `source_row` is the raw CSV row exactly as it was
        # read, kept verbatim so the sidecar can hand downstream tools every column
        # the sheet carried, including ones this app has no opinion about.
        self.source_row = kwargs.get('source_row') or {}
        self.tile_mm_width = kwargs.get('tile_mm_width')
        self.tile_mm_height = kwargs.get('tile_mm_height')
        self.pitch_mm = kwargs.get('pitch_mm')
        self.product = kwargs.get('product')
        self.physical_source = kwargs.get('physical_source', PHYSICAL_UNKNOWN)

    def set_physical_from_tile(self, tile, source=PHYSICAL_FROM_REPO):
        """Adopt physical dimensions from a tile repository record."""
        if not tile:
            return
        self.tile_mm_width = parse_number(tile.get('physical_width'))
        self.tile_mm_height = parse_number(tile.get('physical_height'))
        self.pitch_mm = parse_number(tile.get('pitch'))
        self.product = tile.get('name') or self.product
        if self.tile_mm_width or self.tile_mm_height:
            self.physical_source = source

    def physical_size_mm(self):
        """(width, height) of the whole screen in mm, or (None, None).

        Recomputed from tile size x tile count rather than read off the sheet:
        tile counts are editable in the app, so the sheet's own screen dimensions
        go stale the moment anyone touches them.
        """
        width = self.tile_mm_width * self.tiles_w if self.tile_mm_width else None
        height = self.tile_mm_height * self.tiles_h if self.tile_mm_height else None
        return width, height

    def tile_counts(self):
        """(total, enabled, disabled) tile counts across the enabled_array."""
        rows = math.ceil(self.tiles_h)
        cols = math.ceil(self.tiles_w)
        total = rows * cols
        enabled = 0
        for i in range(rows):
            if i >= len(self.enabled_array):
                continue
            for j in range(cols):
                if j < len(self.enabled_array[i]) and self.enabled_array[i][j]:
                    enabled += 1
        return total, enabled, total - enabled

    def enabled_map(self):
        """Serialize enabled_array as '110;011' - same format save_to_csv writes."""
        return ';'.join(
            ''.join(str(int(bool(val))) for val in row)
            for row in self.enabled_array
        )


def resolve_physical_from_repo(screen, tiles=None):
    """Fill in missing physical dimensions from the LED tile repository.

    Only touches screens the eng sheet gave us nothing for. Matches on pixel
    dimensions, preferring a tile whose name or brand also matches the sheet's
    Product column so that two tiles sharing a pixel count don't get confused.
    """
    if screen.tile_mm_width and screen.tile_mm_height:
        return screen

    if tiles is None:
        try:
            from database import DatabaseManager
            tiles = DatabaseManager().get_all_tiles()
        except Exception as e:
            logger.warning("Could not read tile repository: %s", e)
            return screen

    candidates = [
        t for t in tiles
        if parse_number(t.get('pixel_width')) == float(screen.tile_width)
        and parse_number(t.get('pixel_height')) == float(screen.tile_height)
    ]
    if not candidates:
        return screen

    # Narrow by the sheet's Product string when it helps.
    product = (screen.product or '').strip().lower()
    if product:
        named = [
            t for t in candidates
            if product in str(t.get('name', '')).lower()
            or product in str(t.get('brand', '')).lower()
            or str(t.get('name', '')).lower() in product
        ]
        if named:
            candidates = named

    if len(candidates) > 1:
        # Several tiles share this pixel size and nothing disambiguates them.
        # Reporting unknown beats guessing a physical size that ends up in a
        # drawing someone builds a wall from.
        logger.info(
            "Skipping repo lookup for '%s': %d tiles share %sx%s px",
            screen.name, len(candidates), screen.tile_width, screen.tile_height,
        )
        return screen

    screen.set_physical_from_tile(candidates[0])
    return screen


class ScreenDrawer:
    def __init__(self, screen: Screen, path) -> None:
        self.screen = screen
        self.path = path
        self.name = screen.name
        self.width = int(screen.tile_width * screen.tiles_w) 
        self.height = int(screen.tile_height * screen.tiles_h)
        self.tile_width = screen.tile_width
        self.tile_height = screen.tile_height
        self.tiles_w = screen.tiles_w
        self.tiles_h = screen.tiles_h
        self.colorBGHue = screen.colorBGHue
        self.colorA = blue
        self.colorB = red
        self.num = screen.num
        self.im = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        self.draw = ImageDraw.Draw(self.im)
        self.filename = self.sanitize_filename(self.name)
        self.error = False


    def draw_eng(self):
        self.resetImg()
        self.draw_tiles()
        self.draw_screen_text()
        self.im.save(self.path / '02_Eng_Blocks' / ("{:03d}_{}.png".format(self.num, self.name)))
        pass


    def draw_content(self):
        self.resetImg()
        self.drawBG(gray, red)
        self.draw_screen_text()
        self.im.save(self.path / '01_Content_Blocks' / (("{:03d}_{}.png".format(self.num, self.name))))
        pass

    def draw_stealth(self):
        self.resetImg()
        self.drawBG((0,0,0), (76,185,227))
        self.im.save(self.path / '03_Stealth_Blocks' / (("{:03d}_{}.png".format(self.num, self.name))))
        pass

    def draw_pretty(self):
        self.resetImg()
        pass

    def resetImg(self):
        self.im = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        self.draw = ImageDraw.Draw(self.im)

    def drawBG(self, color, outline_color):
        total_rows = math.ceil(self.tiles_h)
        total_cols = math.ceil(self.tiles_w)

        cur_x = 0
        cur_y = 0

        for i in range(total_rows):
            remaining_h = self.tiles_h - i
            tile_y_height = self.tile_height * (0.5 if 0 < remaining_h < 1 else 1)

            for j in range(total_cols):
                remaining_w = self.tiles_w - j
                tile_x_width = self.tile_width * (0.5 if 0 < remaining_w < 1 else 1)

                if self.screen.enabled_array[i][j]:
                    # 1. Fill the background color for the enabled tile
                    x1, y1 = cur_x, cur_y
                    x2, y2 = cur_x + tile_x_width, cur_y + tile_y_height
                    self.draw.rectangle((x1, y1, x2 - 1, y2 - 1), fill=color)

                    # 2. Draw borders only on external edges
                    # Check Top
                    if i == 0 or not self.screen.enabled_array[i - 1][j]:
                        self.draw.line((x1, y1, x2 - 1, y1), fill=outline_color, width=1)

                    # Check Bottom
                    if i == total_rows - 1 or not self.screen.enabled_array[i + 1][j]:
                        self.draw.line((x1, y2 - 1, x2 - 1, y2 - 1), fill=outline_color, width=1)

                    # Check Left
                    if j == 0 or not self.screen.enabled_array[i][j - 1]:
                        self.draw.line((x1, y1, x1, y2 - 1), fill=outline_color, width=1)

                    # Check Right
                    if j == total_cols - 1 or not self.screen.enabled_array[i][j + 1]:
                        self.draw.line((x2 - 1, y1, x2 - 1, y2 - 1), fill=outline_color, width=1)

                cur_x += tile_x_width
            cur_x = 0
            cur_y += tile_y_height

    def draw_tiles(self):
        cur_x = 0
        cur_y = 0

        BG_A = self.hsv_to_rgb(self.colorBGHue, 100, 30)
        BG_B = gray2

        font_size = int(min(self.height,self.width)/6)
        font = ImageFont.truetype(root_dir / 'lib'/ 'font' /'RobotoMono-Light.ttf', font_size)

        for i in range(math.ceil(self.tiles_h)):
            remaining_h = self.tiles_h - i
            tile_y_height = self.tile_height * (0.5 if 0 < remaining_h < 1 else 1)

            for j in range(math.ceil(self.tiles_w)):
                remaining_w = self.tiles_w - j
                tile_x_width = self.tile_width * (0.5 if 0 < remaining_w < 1 else 1)

                BG_color = BG_A if (j+i)%2 == 0 else BG_B
                stoke = red if (j+i)%2 == 0 else blue
                if self.screen.enabled_array[i][j]:
                    self.draw.rectangle((cur_x, cur_y, cur_x+(tile_x_width-1), cur_y+(tile_y_height-1)), BG_color, stoke, 1)
                    self.draw.text((cur_x+tile_x_width/2, cur_y+tile_y_height/2), f"{j+1},{i+1}", (255,255,255), anchor='mm')

                cur_x += tile_x_width

            cur_x = 0
            cur_y += tile_y_height

    def draw_simple_tiles(self, bgColor, strokeColor):
        cur_x = 0
        cur_y = 0

        BG_A = bgColor

        for i in range(math.ceil(self.tiles_h)):
            remaining_h = self.tiles_h - i
            tile_y_height = self.tile_height * (0.5 if 0 < remaining_h < 1 else 1)

            for j in range(math.ceil(self.tiles_w)):
                remaining_w = self.tiles_w - j
                tile_x_width = self.tile_width * (0.5 if 0 < remaining_w < 1 else 1)

                BG_color = BG_A
                stoke = strokeColor

                self.draw.rectangle((cur_x, cur_y, cur_x+(tile_x_width-1), cur_y+(tile_y_height-1)), BG_color, stoke, 1)

                cur_x += tile_x_width

            cur_x = 0
            cur_y += tile_y_height

    def draw_screen_text(self):
        res_text = str(self.width) + "x" + str(self.height)
        font = self.draw.getfont()
        font_size = int(min(self.height,self.width)/max(len(self.name),len(res_text)))

        print("FontSize:"+str(font_size))

        font = ImageFont.truetype(root_dir / 'lib'/ 'font' /'RobotoMono-Light.ttf', font_size)

        self.draw.text((self.width/2,self.height/2), self.name.strip(), font=font, fill=(255,255,255), anchor='md')
        self.draw.text((self.width/2,self.height/2), res_text, font=font, fill=(255,255,255), anchor='ma')

    def get_text_dimensions(self, text_string, font):
        ascent, descent = font.getmetrics()
        text_width = font.getmask(text_string).getbbox()[2]
        text_height = font.getmask(text_string).getbbox()[3] + descent
        return (text_width, text_height)

    def hsv_to_rgb(self, h,s,v):
        rgb_fraction = colorsys.hsv_to_rgb(h / 360, s / 100, v / 100)
        rgb = tuple(int(i * 255) for i in rgb_fraction)
        return rgb

    def sanitize_filename(self, filename, max_length=255):
        invalid_chars = r'[<>:"/\\|?*\x00-\x1F]'
        invalid_mac_char = r'[:]' 
        sanitized = re.sub(invalid_chars, '_', filename)
        sanitized = re.sub(invalid_mac_char, '_', sanitized)
        sanitized = sanitized.strip()

        reserved_windows_names = {
            "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5", 
            "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2", "LPT3", "LPT4", 
            "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"
        }
        if sanitized.upper() in reserved_windows_names:
            sanitized += '_'
        if len(sanitized) > max_length:
            sanitized = sanitized[:max_length]
        return sanitized

class ScreenList:
    def __init__(self, csv_path) -> None:
        self.screens = []
        self.rawScreens = self.parse_csv_with_header(csv_path) 
        if self.screens == []: 
            print ("Parse Failed. Returning with Nothing")
            return None
        else:
            self.setBGColors()

    def parse_csv_with_header(self, csv_path):
        required_columns = [
            "WALL", "Tile_Px_Width", "Tile_Px_Height", "Tiles_Wide", "Tiles_High" 
        ]

        try:
            header_row = None
            header_index = -1
            with open(csv_path, mode='r', encoding='utf-8') as file:
                csv_reader = csv.reader(file)
                for i, row in enumerate(csv_reader):
                    row_stripped = [h.strip() for h in row]
                    if all(col in row_stripped for col in required_columns):
                        header_row = row_stripped
                        header_index = i
                        break

            if header_row is None:
                print(f"Error: CSV is missing required columns. Required: {required_columns}")
                self.error = True
                return None

            # Now re-open and skip to the header row
            with open(csv_path, mode='r', encoding='utf-8') as file:
                csv_reader = csv.reader(file)
                for _ in range(header_index):
                    next(csv_reader)
                dict_reader = csv.DictReader(file)
                print("------Dict Reader Initialized with Fieldnames------")
                print(dict_reader.fieldnames)

                for row in dict_reader:
                    try:
                        if row["WALL"].strip():
                            
                            enabled_array = None
                            if 'Enabled_Array' in row and row['Enabled_Array']:
                                try:
                                    # Deserialize the enabled_array string
                                    enabled_array = [[bool(int(c)) for c in r] for r in row['Enabled_Array'].split(';')]
                                except (ValueError, TypeError):
                                    print(f"Warning: Could not parse Enabled_Array for {row['WALL']}. Defaulting to all enabled.")

                            screen_kwargs = {
                                'num': len(self.screens)
                            }
                            if enabled_array is not None:
                                screen_kwargs['enabled_array'] = enabled_array

                            # Keep the whole sheet row, plus typed copies of the
                            # engineering columns the app can actually reason about.
                            source_row = {k: v for k, v in row.items() if k is not None}
                            tile_mm_width = parse_number(row.get(ENG_TILE_MM_WIDTH))
                            tile_mm_height = parse_number(row.get(ENG_TILE_MM_HEIGHT))
                            product = (row.get(ENG_PRODUCT) or '').strip() or None

                            screen_kwargs.update({
                                'source_row': source_row,
                                'tile_mm_width': tile_mm_width,
                                'tile_mm_height': tile_mm_height,
                                'pitch_mm': parse_number(row.get(ENG_PITCH_MM)),
                                'product': product,
                                'physical_source': (
                                    PHYSICAL_FROM_SHEET
                                    if (tile_mm_width and tile_mm_height)
                                    else PHYSICAL_UNKNOWN
                                ),
                            })

                            print(f"Adding screen with name: {row['WALL']}")
                            self.screens.append(
                                Screen(
                                    row["WALL"],
                                    float(row['Tile_Px_Width']),
                                    float(row['Tile_Px_Height']),
                                    float(row['Tiles_Wide']),
                                    float(row['Tiles_High']),
                                    **screen_kwargs
                                )
                            )
                    except (ValueError, KeyError) as e:
                        print(f"Skipping row due to error: {e}")

        except FileNotFoundError:
            print(f"Error: File '{csv_path}' not found.")
            return None
        except Exception as e:
            print(f"An error occurred: {e}")
            return None

    def save_to_csv(self, file_path):
        header = ["WALL", "Tile_Px_Width", "Tile_Px_Height", "Tiles_Wide", "Tiles_High", "Enabled_Array"]
        try:
            with open(file_path, 'w', newline='', encoding='utf-8') as file:
                writer = csv.writer(file)
                writer.writerow(header)
                for screen in self.screens:
                    # Serialize the enabled_array into a string format like "110;111;011"
                    enabled_str = ';'.join([''.join([str(int(val)) for val in row]) for row in screen.enabled_array])
                    
                    row_data = [screen.name, screen.tile_width, screen.tile_height, screen.tiles_w, screen.tiles_h, enabled_str]
                    writer.writerow(row_data)
            print(f"Successfully saved screen data to {file_path}")
            return True
        except Exception as e:
            print(f"Error saving CSV file: {e}")
            return False

    def setBGColors(self):
        coloroffset = int(360 / len(self.screens))
        curcol = 0
        for i in self.screens:
            i.colorBGHue = curcol
            curcol += coloroffset


def test(csv_name='LP.csv'):
    # Imported here rather than at module scope: sidecar imports from this module.
    from sidecar import write_sidecar

    print(root_dir)
    csv_path = root_dir / 'temp' / csv_name
    filename = os.path.basename(csv_path).split('.')[0]
    path = root_dir / 'testing' / filename

    List = ScreenList(csv_path)
    print(List.screens)

    os.makedirs(root_dir / 'testing' / filename / '01_Content_Blocks', exist_ok=True)
    os.makedirs(root_dir / 'testing' / filename / '02_Eng_Blocks', exist_ok=True)
    os.makedirs(root_dir / 'testing' / filename / '03_Stealth_Blocks', exist_ok=True)

    for i in List.screens: 
        print(i.name)
        testing = ScreenDrawer(i,path)
        testing.draw_content()
        testing.draw_eng()
        testing.draw_stealth()

    print(f"Wrote sidecar: {write_sidecar(List.screens, path, csv_path)}")

if __name__ == "__main__":
    test(sys.argv[1] if len(sys.argv) > 1 else 'LP.csv')
