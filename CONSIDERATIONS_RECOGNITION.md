# Cambios en TCVIEW

- En la pestaña "Image & Calibration" en el menú calibration debe poder seleccionarse una etapa y con base en la etapa seleccionada desplegar las calibraciones pertinentes, así por ejemplo, si elijo la etapa 3 no debería mostrarse controles calibraciones de HSV, las calibraciones de esta deben ser según el modelo previo con el que se tomarán datos. 

- Para el zoom que actualmente está discretizado en x2, x3 y x4 cambialo por lupas - y más para poder ajustarlo más adecuadamente. Este cambio también debe aplicarse a "Label Frames"

- En "Label Frames" añade una opción de colorear interior que haga lo siguiente: si dibujo un patrón de etiquetado cerrado con esta opción puedo completar el interior de esta región también. 

- En "Flight data" debo poder cambiar la vista de "Field of view" por la imagen de la cámara con las etiqueta de los blobs. 

# Adiciones a TCVIEW

- Necesito crear una pestaña de entrenamiento, esta pestaña debe permitir seleccionar una o varias carpetas de datos y una etapa de reconocimiento (0,1,2,3) y tener un botón para realizar el entrenamiento. EL archivo resultante de este entrenamiento debe poder ser nombrado por el usuario y guardarse en la carpeta @firmware/projects/targetcam/models. Por defecto la carpeta de selección de datos será @firmware/projects/targetcam/data.

- Pestaña de verificación y comparación: Esta pestaña debe permitir tomar un modelo de reconocimiento, sin importar en que etapa se encuentre y una o varias carpeta de testeo. Debe realizar una evaluación de el modelo elegido con los datos seleccionados y poder visualizar sus métricas (precisión, recall, FP, FN, visualización de las imágenes con la detección del modelo, etc.). También debe permitir seleccionar dos modelos. La GUI debe sugerir si según las métricas la tabla es buena y entre dos tablas cuál es más recomendable si dichas conclusiones pueden realizarse.

- Pestaña de compilación y carga en el ESP32, el modelo que esté actualmente cargado en el MCU debe respaldarse en la carpeta models para que no se pierda. 

# Aclaración sobre train_gui y la rama recognition_interface:

La rama recognition_interface será eliminada, sin embargo vamos a implementar en esta rama lo que ya está implementado ahí. Sin embargo esta será usada como referencia, no haremos una copia exacta.
El principal cambio es que solo tendremos una GUI (TCVIEW) y aquí se implementarán las demás ventanas.


Antes de continuar dame retroalimentación de las intrucciones dadas y comenta posibles inconsistencias. Luego de esto continuaremos pues con el plan en @RECOGNITION.md teniendo en cuenta las cosideraciones de este archivo.  
