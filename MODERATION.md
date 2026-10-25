# Moderación de fotos: cambio propuesto en rama de pruebas

Rama: `fix/moderacion-album-originales`. Base: `45689f21aab7a4c86f43cc6478c03818d8f179d5`.
Este documento describe código de prueba; no confirma su publicación en producción.

## Comportamiento

- Sólo se acepta el nombre exacto de una preview UUID de 32 caracteres hexadecimales y extensión `.jpg`.
- Se oculta la preview local y se guarda un veto en `Datos/moderation-<uuid>.json` de Drive. Se verifica checksum y lectura del veto antes de borrar archivos remotos.
- Se buscan originales por UUID exacto en `Originales`, con todas las páginas. Se guardan sus IDs en el veto antes de eliminarlos.
- Se eliminan todos los originales identificados, las previews exactas y todas las copias accesibles vinculadas mediante `appProperties.sourceFileId`, aunque estén renombradas o en diferentes carpetas.
- El veto conserva los IDs después del borrado. Repetir la operación permite completar fallos parciales; un DELETE 404 de un ID ya registrado se trata como ausente. Otros errores devuelven HTTP 503 y `ok:false`.
- El loop, las URLs de previews, la restauración, la copia automática, la reconciliación y la galería consultan el veto. El álbum excluye las copias también de descarga directa y ZIP. Los vetos no se limpian al cambiar de evento.
- El panel muestra errores y mantiene un botón para reintentar la foto pendiente, incluso al recargar ese navegador.

## Verificación aislada

Instalar las dependencias habituales del proyecto y ejecutar:

```sh
python3 -m unittest discover -s tests -v
node tests/test_admin_moderation.js
```

Las pruebas de moderación usan un Drive en memoria, IDs y contenido ficticios, carpetas temporales, credenciales deshabilitadas e importación sin hilo de restauración. Las conexiones de red están bloqueadas durante esos casos.

## Límites y requisitos antes de producción

- Actualizar tanto el servicio de pantalla como el servidor independiente del álbum con esta versión de `delivery.py` y `gallery.py`. Ambos deben usar la misma carpeta `Datos`; no hay caché local de vetos en la galería.
- Si Drive no permite persistir el veto, sólo queda la exclusión local temporal y la operación falla explícitamente. No hay garantía de persistencia hasta un reintento exitoso.
- Si falla la lectura de vetos, se bloquea la publicación de fotos en esa respuesta. Esto agrega lecturas de Drive; no se midió carga con un álbum grande.
- Las copias ya descargadas y los bytes de una descarga en curso no se pueden retirar. El loop retira la foto en su próxima consulta habitual (8 segundos); no se modificaron tiempos, transiciones ni integración de video/NDI.
- La prueba real de Drive, el panel en navegador y la salida NDI son NO VERIFICABLES en esta ejecución aislada. No se desplegó ni se borraron fotos reales.
- No borrar los archivos `moderation-*.json` al limpiar eventos: constituyen el veto persistente. La limpieza automática actual sólo elimina previews antiguas y `mensajes.json`.
