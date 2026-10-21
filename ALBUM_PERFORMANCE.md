# Álbum familiar: miniaturas persistentes

Implementación propuesta en `fix/moderacion-album-originales`, sobre la corrección de moderación `b916ff7`. No confirma un despliegue.

## Cómo funciona

La cuadrícula y el visor usan JPEG optimizado y progresivo, de hasta 512 píxeles y calidad 78. JPEG permite reutilizar Pillow sin incorporar codecs ni dependencias de producción adicionales. La descarga individual y el ZIP siguen leyendo los archivos completos, sin conversión ni compresión adicional.

CSS y JavaScript llevan una versión en su URL para evitar reutilizar el cargador anterior después de publicar la optimización. El proxy del servidor debe permitir `album_loader.js`, las rutas `miniatura/<id>` y `estado`, manteniendo las restricciones del resto de rutas.

Cada miniatura se guarda en `Entregas/<evento>/Miniaturas`, fuera de `Fotos originales`: así no altera los conteos de entrega ni se confunde con una copia durante la reconciliación. El nombre identifica la versión del generador, el ID de la foto y su checksum. Una visita posterior o un reinicio reutiliza ese archivo en Drive. Se genera únicamente lo solicitado, sin procesar todo un álbum durante una visita.

Las miniaturas incluyen `appProperties.sourceFileId`, igual que las copias familiares. El moderador existente encuentra y elimina también estos archivos sin modificar `app.py`, `delivery.py` ni el panel. La carpeta técnica no aparece en el álbum ni entra en el ZIP.

El navegador solicita primero dos fotos y mantiene hasta dos solicitudes de miniaturas de la cuadrícula en curso. Las demás se habilitan al acercarse a la zona visible, usando IntersectionObserver y una alternativa por desplazamiento para navegadores sin esa API. El visor solicita sólo la foto elegida y cancela una petición anterior al navegar rápido.

## Caché, moderación y límites

- Los bytes de miniaturas tienen caché LRU de hasta 8 MiB por proceso.
- El índice de fotos dura 15 segundos, con límites de 8 álbumes y 20.000 filas. No descarga originales para mostrar el HTML.
- Los vetos se consultan en Drive antes de mostrar fotos o aceptar una validación HTTP. Sólo se reutilizan cuerpos JSON cuyo checksum no cambió; no hay una ventana positiva de caché para autorizar una foto moderada.
- `Cache-Control: private, no-cache, must-revalidate` y ETag permiten 304 después de validar acceso, pertenencia y veto. Descargas completas y demás respuestas del álbum conservan `private, no-store`.
- Una foto vetada se excluye incluso si falla el borrado de su miniatura. Sus bytes salen también de la caché de memoria al detectar el veto. Un álbum abierto consulta disponibilidad cada 30 segundos mientras está visible y al volver a la pestaña; el retorno desde la caché de navegación recarga la página.
- Un veto que llega durante la generación impide publicar la miniatura. Si llega inmediatamente después de guardarla, se intenta retirarla y la respuesta no muestra éxito ante un error.
- La generación descarga a un archivo temporal limitado a 32 MiB. Pillow reduce JPEG durante la decodificación cuando es posible; se rechazan vistas previas que todavía requieren más de 12 millones de píxeles decodificados. El original sigue descargable. Cada miniatura queda limitada a 256 KiB.
- Se procesa una generación por proceso. Bloqueos de archivo coordinan los workers del mismo servidor y evitan generar dos veces la misma foto. Son 64 archivos de bloqueo, no uno por foto. La persistencia está en Drive; el disco temporal no guarda la única copia.

## Preparación antes de compartir

La primera petición de una foto sin miniatura todavía debe leer su original una vez. Para evitar esa espera a la primera familia, se puede preparar el álbum en el servidor que lo sirve, usando su entorno existente:

```sh
python prepare_album_thumbnails.py --slug IDENTIFICADOR_DEL_ALBUM
```

Este comando crea únicamente miniaturas, trabaja secuencialmente, reutiliza las existentes y respeta vetos. Informa cantidades `ready`, `vetoed`, `unsupported`, `failed` y `ok`; termina con error si queda alguna vista previa fallida o no compatible. No se ejecutó contra datos reales en esta tarea. No hay cron ni procesamiento nuevo en recepción de fotos o pantallas.

## Servicios y configuración

No se requieren variables de entorno nuevas ni paquetes adicionales. Se reutilizan las credenciales y permisos de Drive del servidor del álbum, que deben permitir crear archivos en la carpeta del evento.

La mejora corresponde al servicio que sirve el álbum familiar. Este repositorio tiene un servidor independiente `album_app.py` y también rutas de álbum en `app.py`. Hay que actualizar todos los servidores que efectivamente sirvan esas rutas, incluyendo `album_media.py` y los estáticos. No es necesario cambiar la configuración de los dos servicios de pantallas en Render para aprovechar la mejora en el servidor independiente del álbum. Deben conservar la corrección anterior de moderación y compartir el Drive correcto.

No se cambiaron recepción de fotos, APIs del loop, plantillas de reproducción, saludos, VJ, OBS, Serato o NDI. No se cambiaron archivos de despliegue ni variables de producción.

## Verificación

```sh
python -m unittest discover -s tests -v
node tests/test_admin_moderation.js
node tests/test_album_loader.js
```

Las pruebas usan fotos sintéticas, Drive en memoria, credenciales desactivadas y conexiones de red bloqueadas. Cubren generación, orientación EXIF, reutilización tras reinicio, workers concurrentes, checksum, fallos, límites, veto y eliminación, caché HTTP, originales y ZIP exactos, preparación y un álbum con 1.000 fotos. Las pruebas anteriores deben seguir pasando sin modificarlas.

## Riesgos pendientes antes de desplegar

- La velocidad real depende de Drive y la conexión del visitante. Las mediciones locales simuladas no son una medición de producción.
- La primera generación de cada foto sigue teniendo costo de lectura y procesamiento; la preparación opcional evita trasladar esa espera a la familia.
- Fotos RAW o muy grandes pueden no ofrecer miniatura. La descarga completa sigue disponible.
- Los bloqueos cubren workers de un mismo servidor. Con réplicas en diferentes contenedores o servidores podría haber generación simultánea. Antes de habilitar múltiples generadores se requiere coordinación compartida; los duplicados detectados se rechazan en vez de servir un resultado ambiguo.
- El número de consultas de veto crece con las páginas de vetos existentes. Se prioriza su comprobación actual sobre servir contenido potencialmente moderado. No se midieron cuotas reales de Drive ni memoria máxima del servicio en producción.
- Las fotos ya vistas o descargadas no se pueden retirar del dispositivo del destinatario. Una pestaña abierta retira fotos moderadas en su próxima comprobación de disponibilidad, hasta 30 segundos; cualquier petición nueva queda bloqueada desde que el veto es visible en Drive.
- Antes de producción se requiere autorización y una prueba controlada en el servidor real. No se desplegó ni se eliminaron fotos reales durante esta tarea.

Referencias: [consultas y propiedades de Drive](https://developers.google.com/workspace/drive/api/guides/search-files), [decodificación JPEG de Pillow](https://pillow.readthedocs.io/en/stable/handbook/image-file-formats.html#jpeg), [revalidación HTTP](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Cache-Control).

## Revisión móvil — 2026-10-21 (pendiente de publicación)

La medición del servidor vigente dio una miniatura HTTP 200 en 1,521 s y su revalidación 304 en 0,289 s. Son mediciones puntuales desde el servidor, no una simulación de red móvil. Los bytes ya eran pequeños; había consultas redundantes de moderación y descargas de la misma miniatura por distintos workers.

Esta revisión reutiliza los bytes de memoria inmediatamente después de la comprobación actual de pertenencia y veto de `photos()`. Una respuesta 200 caliente requiere una sola consulta de metadatos de veto, igual que un 304, sin lecturas binarias ni conversión. No se cachea la autorización ni el veto positivo.

Sólo `album_app.py` habilita además una caché de disco compartida entre workers, con escrituras atómicas, checksum local y expulsión por antigüedad a 128 MiB. El segundo worker puede reutilizar la miniatura descargada por el primero. Drive conserva la copia persistente. Disco lleno, permisos o archivo corrupto vuelven a Drive; un fallo de comprobación de veto devuelve 503 aun existiendo bytes locales. Un veto detectado elimina también la entrada local y bloquea memoria y disco de cualquier worker.

No hay variables obligatorias nuevas. Opcional: `ALBUM_THUMBNAIL_CACHE_DIR`, directorio privado y escribible del servicio del álbum. Sin definirlo usa su directorio temporal privado: sobrevive a recargas de workers, pero puede vaciarse al reiniciar el servicio. Para conservarlo entre reinicios completos se debe configurar un directorio persistente permitido por systemd/Render. No es necesario modificar ningún servicio de pantallas de Render. La persistencia durable sigue en Drive incluso si se pierde esta caché.

Se retiraron los rótulos “Momento 01, 02…” de las tarjetas, conservando los nombres accesibles de las fotos y la descarga. CSS y JS pasan a `album-512-v2` para renovar recursos al publicar.

Validación: 47 pruebas Python aprobadas (las 42 anteriores más 5 nuevas), 0 fallos; las dos suites JavaScript aprobadas. Nuevas comprobaciones: una consulta por 200 caliente; segundo worker sin descargas binarias; veto/fallo de Drive bloquean caché de disco; recuperación de entrada corrupta; límite de disco y fallo de escritura. Fotos ficticias y conexiones reales bloqueadas. No se publicó esta revisión ni se modificó configuración de producción. Pendiente medir el resultado real en móvil tras una publicación autorizada.
