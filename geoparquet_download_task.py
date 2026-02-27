from qgis.core import QgsTask, QgsMessageLog, Qgis, QgsVectorLayer, QgsProject, QgsApplication
from qgis.PyQt.QtCore import QVariant, pyqtSignal
import requests
import os

class GeoparquetDownloadTask(QgsTask):

    downloadFinished = pyqtSignal(str)

    def __init__(self, description, url, path, obj_type):
        super().__init__(description, QgsTask.CanCancel)
        self.url = url
        self.path = path
        self.obj_type = obj_type
        self.exception = None

    def run(self):
        """Tutaj odbywa się pobieranie (w osobnym wątku)"""
        try:
            CHUNK_SIZE = 1024 * 1024 * 5  # 1 MB * 5
            response = requests.get(self.url, stream=True, timeout=900)
            response.raise_for_status()

            total_size = int(response.headers.get('content-length', 0))
            downloaded_size = 0

            with open(self.path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                    # Sprawdź, czy użytkownik nie anulował zadania w QGIS
                    if self.isCanceled():
                        return False
                    
                    if chunk:
                        f.write(chunk)
                        downloaded_size += len(chunk)
                        
                        if total_size > 0:
                            progress = (downloaded_size / total_size) * 100
                            self.setProgress(progress)
            return True
        except Exception as e:
            self.exception = e
            return False

    def finished(self, result):
        """Wywołuje się w głównym wątku po zakończeniu run()"""
        if result:
            QgsMessageLog.logMessage(f"Pobieranie zakończone: {self.path}", "PobieranieEGIB", Qgis.Success)
            # Wywołujemy funkcję wczytującą warstwę (musi być w wątku głównym)
            self.downloadFinished.emit(self.path)
            # self.load_layer()
        else:
            if self.isCanceled():
                QgsMessageLog.logMessage("Pobieranie anulowane przez użytkownika.", "PobieranieEGIB", Qgis.Warning)
            else:
                QgsMessageLog.logMessage(f"Błąd pobierania: {self.exception}", "PobieranieEGIB", Qgis.Critical)
            
            self.downloadFinished.emit('')

    def load_layer(self):
        layer_name = os.path.basename(self.path)
        vlayer = QgsVectorLayer(self.path, layer_name, "ogr")
        if vlayer.isValid():
            QgsProject.instance().addMapLayer(vlayer)
        else:
            QgsMessageLog.logMessage("Nie udało się wczytać pliku Parquet po pobraniu.", "PobieranieEGIB", Qgis.Critical)