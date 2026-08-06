import sys
from pathlib import Path
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QTabWidget, QVBoxLayout, 
    QPushButton, QLabel, QFileDialog, QHBoxLayout, QListWidget, QLineEdit,
    QGridLayout, QGroupBox, QComboBox
)
from PySide6.QtGui import QPixmap, QPainter, QColor, QPen, QFont
from PySide6.QtCore import Qt, QRectF, QPointF
import math
import screens
import sidecar
from settings import Settings

class ImageViewer(QWidget):
    def __init__(self, image_path, parent=None):
        super().__init__(parent)
        
        layout = QVBoxLayout(self)

        # Display image
        label = QLabel(self)
        pixmap = QPixmap(image_path)
        label.setPixmap(pixmap)
        #label.setAlignment(Qt.AlignCenter)
        
        layout.addWidget(label)

class ScreenPreviewWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.ledScreen = None
        self.setMinimumSize(400, 300)
        self.tile_rects = []
        self.offset_x = 0
        self.offset_y = 0
        self.last_toggled_tile = None

    def set_screen(self, screen):
        self.ledScreen = screen
        self.update() # Trigger a repaint

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self.ledScreen:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # Calculate scaling factor to fit the widget
        widget_rect = self.rect().adjusted(10, 10, -10, -10) # Add padding
        scale_w = widget_rect.width() / self.ledScreen.width
        scale_h = widget_rect.height() / self.ledScreen.height
        scale = min(scale_w, scale_h)

        scaled_width = self.ledScreen.width * scale
        scaled_height = self.ledScreen.height * scale

        # Center the drawing
        self.offset_x = (self.width() - scaled_width) / 2
        self.offset_y = (self.height() - scaled_height) / 2

        painter.translate(self.offset_x, self.offset_y)

        self.tile_rects.clear()
        cur_y = 0
        for i in range(math.ceil(self.ledScreen.tiles_h)):
            tile_h = self.ledScreen.tile_height * (0.5 if 0 < self.ledScreen.tiles_h - i < 1 else 1)
            cur_x = 0
            for j in range(math.ceil(self.ledScreen.tiles_w)):
                tile_w = self.ledScreen.tile_width * (0.5 if 0 < self.ledScreen.tiles_w - j < 1 else 1)
                
                tile_rect = QRectF(cur_x * scale, cur_y * scale, tile_w * scale, tile_h * scale)
                self.tile_rects.append(((i, j), tile_rect))

                is_enabled = self.ledScreen.enabled_array[i][j]
                
                painter.setPen(QPen(QColor("white"), 1))
                painter.setBrush(QColor(50, 50, 50) if is_enabled else QColor(10, 10, 10))
                painter.drawRect(tile_rect)

                if not is_enabled:
                    painter.setPen(QPen(QColor("red"), 2))
                    painter.drawLine(tile_rect.topLeft(), tile_rect.bottomRight())
                    painter.drawLine(tile_rect.topRight(), tile_rect.bottomLeft())

                cur_x += tile_w
            cur_y += tile_h

    def mousePressEvent(self, event):
        # Adjust click position by the same offset used for drawing
        click_pos = event.position()
        transformed_pos = click_pos - QPointF(self.offset_x, self.offset_y)

        for (i, j), rect in self.tile_rects:
            if rect.contains(transformed_pos):
                self.ledScreen.enabled_array[i][j] = not self.ledScreen.enabled_array[i][j]
                self.last_toggled_tile = (i, j) # Keep track of the last tile toggled
                self.update() # Repaint to show the change
                break

    def mouseMoveEvent(self, event):
        # Check if the left mouse button is being held down
        if not (event.buttons() & Qt.LeftButton):
            return

        click_pos = event.position()
        transformed_pos = click_pos - QPointF(self.offset_x, self.offset_y)

        for (i, j), rect in self.tile_rects:
            if rect.contains(transformed_pos):
                # Only toggle if it's a new tile in the drag sequence
                if (i, j) != self.last_toggled_tile:
                    self.ledScreen.enabled_array[i][j] = not self.ledScreen.enabled_array[i][j]
                    self.last_toggled_tile = (i, j)
                    self.update()
                break

    def mouseReleaseEvent(self, event):
        # Reset the tracking when the mouse button is released
        self.last_toggled_tile = None

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        # Main layout
        self.setWindowTitle("F9 RasterMakker")
        self.setGeometry(100, 100, 1500, 1000)

        # Create central widget and main layout
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget) # Main layout is now horizontal

        # Initialize Settings and paths
        self.settings = Settings()
        self.csv_file_path = Path("placeholder.csv") # Initialize with a Path to avoid type errors, but it won't be used until set
        self.csv_file_path = None # Re-set to None, but now the type hint system should be happier or we can just use Optional
        self.output_folder_path = None
        self.ledScreen_list = None

        # Left sidebar for screen list
        self.ledScreen_list_widget = QListWidget()
        self.ledScreen_list_widget.setMaximumWidth(200) # Set a max width for the sidebar
        self.ledScreen_list_widget.itemSelectionChanged.connect(self.on_screen_selection_changed)
        main_layout.addWidget(self.ledScreen_list_widget)

        # Right side widget for controls and tabs
        right_side_widget = QWidget()
        right_side_layout = QVBoxLayout(right_side_widget)
        main_layout.addWidget(right_side_widget, 1) # The '1' makes it take available space

        # Horizontal layout for the buttons (placed at the top)
        button_layout = QHBoxLayout()

        # CSV file selection button
        self.csv_button = QPushButton("Select CSV File")
        self.csv_button.clicked.connect(self.select_csv_file)
        button_layout.addWidget(self.csv_button)

        # Folder selection button
        self.folder_button = QPushButton("Select Output Folder")
        self.folder_button.clicked.connect(self.select_output_folder)
        button_layout.addWidget(self.folder_button)

        # Run button (disabled by default)
        self.run_button = QPushButton("Run")
        self.run_button.setEnabled(False)  # Disabled initially
        self.run_button.clicked.connect(self.run_image_creation)
        button_layout.addWidget(self.run_button)

        # Save CSV button
        self.save_csv_button = QPushButton("Save CSV")
        self.save_csv_button.setEnabled(False) # Disabled until a CSV is loaded
        self.save_csv_button.clicked.connect(self.save_csv_file)
        button_layout.addWidget(self.save_csv_button)

        # Add the button layout to the main layout at the top
        right_side_layout.addLayout(button_layout)

        # Interactive screen preview widget
        self.ledScreen_preview = ScreenPreviewWidget()
        right_side_layout.addWidget(self.ledScreen_preview, 1) # Give it stretch factor

        # Tab widget for viewing generated images (will be used later)
        self.image_tab_widget = QTabWidget()
        self.image_tab_widget.hide() # Initially hidden

        # Properties box
        properties_group_box = QGroupBox("Screen Properties")
        properties_layout = QGridLayout(properties_group_box)
        
        self.prop_name = QLineEdit()
        self.prop_tile_width = QLineEdit()
        self.prop_tile_height = QLineEdit()
        self.prop_tiles_w = QLineEdit()
        self.prop_tiles_h = QLineEdit()

        # Tile Repository selection
        self.tile_repo_combo = QComboBox()
        self.tile_repo_combo.addItem("Select from Repository...")
        all_tiles = self.settings.get_all_tiles()
        for tile in all_tiles:
            self.tile_repo_combo.addItem(f"{tile['brand']} - {tile['name']}", tile)
        self.tile_repo_combo.currentIndexChanged.connect(self.on_tile_repo_selection_changed)

        properties_layout.addWidget(QLabel("Tile Repository:"), 0, 0)
        properties_layout.addWidget(self.tile_repo_combo, 0, 1, 1, 5)

        # Arrange properties in 3 columns
        properties_layout.addWidget(QLabel("Name:"), 1, 0)
        properties_layout.addWidget(self.prop_name, 1, 1, 1, 5) # Span name across remaining columns

        properties_layout.addWidget(QLabel("Panel Width(px):"), 2, 0)
        properties_layout.addWidget(self.prop_tile_width, 2, 1)
        properties_layout.addWidget(QLabel("Panel Height(px):"), 2, 2)
        properties_layout.addWidget(self.prop_tile_height, 2, 3)
        properties_layout.addWidget(QLabel("Tiles Wide:"), 3, 0)
        properties_layout.addWidget(self.prop_tiles_w, 3, 1)
        properties_layout.addWidget(QLabel("Tiles High:"), 3, 2)
        properties_layout.addWidget(self.prop_tiles_h, 3, 3)

        self.connect_property_signals()

        right_side_layout.addWidget(properties_group_box)

        # Load previous values from settings
        last_csv = self.settings.last_csv_path
        if last_csv:
            self.csv_file_path = Path(last_csv)
            if self.csv_file_path.exists():
                self.populate_screen_list()
                self.save_csv_button.setEnabled(True)
            else:
                self.csv_file_path = None

        last_output = self.settings.default_output_folder
        if last_output:
            self.output_folder_path = Path(last_output)
            self.check_run_button_enabled()
        else:
            self.output_folder_path = None

    def connect_property_signals(self):
        self.prop_name.editingFinished.connect(self.update_screen_from_properties)
        self.prop_tile_width.editingFinished.connect(self.update_screen_from_properties)
        self.prop_tile_height.editingFinished.connect(self.update_screen_from_properties)
        self.prop_tiles_w.editingFinished.connect(self.update_screen_from_properties)
        self.prop_tiles_h.editingFinished.connect(self.update_screen_from_properties)

    def on_tile_repo_selection_changed(self, index):
        if index <= 0:
            return
            
        tile_data = self.tile_repo_combo.itemData(index)
        if tile_data:
            self.prop_tile_width.setText(str(tile_data['pixel_width']))
            self.prop_tile_height.setText(str(tile_data['pixel_height']))
            # Trigger update
            self.update_screen_from_properties()

            # Carry the tile's physical dimensions across too, so a screen built
            # by picking from the repository still reports a real-world size.
            selected_items = self.ledScreen_list_widget.selectedItems()
            if selected_items and self.ledScreen_list:
                current_index = self.ledScreen_list_widget.currentRow()
                if 0 <= current_index < len(self.ledScreen_list.screens):
                    self.ledScreen_list.screens[current_index].set_physical_from_tile(tile_data)

    def select_csv_file(self):
        # Open a file dialog to select a CSV file
        start_dir = ""
        if self.csv_file_path and self.csv_file_path.exists():
            start_dir = str(self.csv_file_path.parent)
        elif self.settings.default_output_folder: # Fallback to a default if CSV never selected
            start_dir = str(Path(self.settings.default_output_folder).parent)
            
        csv_file, _ = QFileDialog.getOpenFileName(self, "Select CSV File", start_dir, "CSV Files (*.csv)")
        if csv_file:
            self.csv_file_path = Path(csv_file)  # Store as Path object
            self.settings.last_csv_path = str(self.csv_file_path) # Save to settings
            print(f"Selected CSV file: {self.csv_file_path}")
            self.populate_screen_list()
            self.check_run_button_enabled()
            self.save_csv_button.setEnabled(True)

    def populate_screen_list(self):
        self.ledScreen_list_widget.clear()
        if not self.csv_file_path or not self.csv_file_path.exists():
            return
        
        self.ledScreen_list = screens.ScreenList(self.csv_file_path)
        for screen in self.ledScreen_list.screens:
            self.ledScreen_list_widget.addItem(screen.name)

    def on_screen_selection_changed(self):
        selected_items = self.ledScreen_list_widget.selectedItems()
        if not selected_items:
            self.ledScreen_preview.set_screen(None)
            for w in [self.prop_name, self.prop_tile_width, self.prop_tile_height, self.prop_tiles_w, self.prop_tiles_h]:
                w.clear()
                w.setEnabled(False)
            return

        selected_name = selected_items[0].text()

        if self.ledScreen_list:
            # Find the screen and populate the QLineEdit fields
            for screen in self.ledScreen_list.screens:
                if screen.name == selected_name:
                    for w in [self.prop_name, self.prop_tile_width, self.prop_tile_height, self.prop_tiles_w, self.prop_tiles_h]:
                        w.setEnabled(True)

                    self.prop_name.setText(screen.name)
                    self.prop_tile_width.setText(str(screen.tile_width))
                    self.prop_tile_height.setText(str(screen.tile_height))
                    self.prop_tiles_w.setText(str(screen.tiles_w))
                    self.prop_tiles_h.setText(str(screen.tiles_h))
                    # Update the preview widget
                    self.ledScreen_preview.set_screen(screen)
                    break

    def update_screen_from_properties(self):
        selected_items = self.ledScreen_list_widget.selectedItems()
        if not selected_items or not self.ledScreen_list:
            return

        # We need the original name to find the object, in case the name itself was changed
        current_index = self.ledScreen_list_widget.currentRow()
        screen_to_update = self.ledScreen_list.screens[current_index]

        try:
            # Update name and list widget item
            new_name = self.prop_name.text()
            if screen_to_update.name != new_name:
                screen_to_update.name = new_name
                selected_items[0].setText(new_name)

            # Update numeric properties with validation
            screen_to_update.tile_width = float(self.prop_tile_width.text())
            screen_to_update.tile_height = float(self.prop_tile_height.text())
            
            new_tiles_w = float(self.prop_tiles_w.text())
            new_tiles_h = float(self.prop_tiles_h.text())

            # Resize enabled_array to match new dimensions
            new_rows = math.ceil(new_tiles_h)
            new_cols = math.ceil(new_tiles_w)
            current_rows = len(screen_to_update.enabled_array)
            current_cols = len(screen_to_update.enabled_array[0]) if current_rows > 0 else 0

            if new_rows != current_rows:
                if new_rows > current_rows:
                    for _ in range(new_rows - current_rows):
                        screen_to_update.enabled_array.append([True] * current_cols)
                else:
                    screen_to_update.enabled_array = screen_to_update.enabled_array[:new_rows]
            
            if new_cols != current_cols:
                for i in range(len(screen_to_update.enabled_array)):
                    if new_cols > current_cols:
                        screen_to_update.enabled_array[i].extend([True] * (new_cols - current_cols))
                    else:
                        screen_to_update.enabled_array[i] = screen_to_update.enabled_array[i][:new_cols]

            screen_to_update.tiles_w = new_tiles_w
            screen_to_update.tiles_h = new_tiles_h

            # Recalculate total width/height
            screen_to_update.width = int(screen_to_update.tile_width * screen_to_update.tiles_w)
            screen_to_update.height = int(screen_to_update.tile_height * screen_to_update.tiles_h)

            # Refresh the preview to reflect size changes
            self.ledScreen_preview.update()

        except ValueError:
            print("Error: Invalid input for screen properties. Please use numbers for dimensions.")
            self.on_screen_selection_changed() # Revert to original values on error

    def save_csv_file(self):
        if not self.ledScreen_list or not self.csv_file_path:
            print("No screen data or CSV path to save.")
            return

        # Suggest a filename based on the original, adding '_modified'
        suggested_name = self.csv_file_path.stem + "_modified.csv"
        save_path, _ = QFileDialog.getSaveFileName(self, "Save CSV File", str(self.csv_file_path.parent / suggested_name), "CSV Files (*.csv)")

        if save_path:
            self.ledScreen_list.save_to_csv(save_path)

    def select_output_folder(self):
        # Open a dialog to select an output folder
        start_dir = ""
        if self.output_folder_path and self.output_folder_path.exists():
            start_dir = str(self.output_folder_path)
            
        folder = QFileDialog.getExistingDirectory(self, "Select Output Folder", start_dir)
        if folder:
            self.output_folder_path = Path(folder)  # Store as Path object
            self.settings.default_output_folder = str(self.output_folder_path) # Save to settings
            print(f"Selected output folder: {self.output_folder_path}")
            self.check_run_button_enabled()

    def check_run_button_enabled(self):
        # Enable the Run button only if both a CSV file and output folder are selected
        if self.csv_file_path and self.output_folder_path:
            self.run_button.setEnabled(True)
        else:
            self.run_button.setEnabled(False)

    def run_image_creation(self):
        # This is where the main logic for creating images will go
        print("Running the main loop to create images...")
        
        # Ensure output folder exists
        if not self.output_folder_path or not self.output_folder_path.exists():
            print(f"Output folder does not exist or not selected: {self.output_folder_path}")
            return
        
        # Prepare output directories
        csv_path = self.csv_file_path
        if not csv_path:
            print("No CSV file selected.")
            return

        output_dir = Path(self.output_folder_path) if self.output_folder_path else Path(self.settings.default_output_folder)
        base_output_path = output_dir

        # Create necessary directories
        (base_output_path / '01_Content_Blocks').mkdir(parents=True, exist_ok=True)
        (base_output_path / '02_Eng_Blocks').mkdir(parents=True, exist_ok=True)
        (base_output_path / '03_Stealth_Blocks').mkdir(parents=True, exist_ok=True)

        # Process each screen
        for screen in self.ledScreen_list.screens:
            print(screen.name)

            # Assuming ScreenDrawer creates images for each screen
            drawer = screens.ScreenDrawer(screen, base_output_path)
            drawer.draw_content()
            drawer.draw_eng()
            drawer.draw_stealth()

        # Drop the XML sidecar alongside the block folders so other tools can
        # pull this run in without going back to the eng sheet.
        try:
            sidecar_path = sidecar.write_sidecar(
                self.ledScreen_list.screens, base_output_path, csv_path
            )
            print(f"Wrote sidecar: {sidecar_path}")
        except Exception as e:
            # A sidecar failure must never cost the user their rendered blocks.
            print(f"Warning: could not write XML sidecar: {e}")

        print("Image creation complete.")
        # Here you could potentially show the image_tab_widget with the results
        # For now, we leave the interactive preview visible.

            # The following code to add images to tabs is commented out as the tab widget
            # is now replaced by the preview. We can add a button to switch views later.
            # sanitized_name = drawer.sanitize_filename(screen.name)
            # content_image = base_output_path / '01_Content_Blocks' / f"{screen.num:03d}_{sanitized_name}.png"
            # eng_image = base_output_path / '02_Eng_Blocks' / f"{screen.num:03d}_{sanitized_name}.png"
            # stealth_image = base_output_path / '03_Stealth_Blocks' / f"{screen.num:03d}_{sanitized_name}.png"
            # self.add_image_tab(str(content_image))
            # self.add_image_tab(str(eng_image))
            # self.add_image_tab(str(stealth_image))

    def add_image_tab(self, image_path):
        if not Path(image_path).exists():
            print(f"Image not found: {image_path}")
            return
        image_viewer = ImageViewer(image_path)
        tab_name = Path(image_path).name  # Use the file name as the tab title
        self.image_tab_widget.addTab(image_viewer, tab_name)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    
    window = MainWindow()

    window.show()
    sys.exit(app.exec())
