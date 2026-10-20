"""Optional album-only preparation before sharing. No writes to original photos."""
import argparse
import json
from album_media import UnsupportedPreview


def prepare(gallery, slug):
    matches = [m for m in gallery.manifests() if m['slug'] == slug]
    if len(matches) != 1:
        raise ValueError('No se encontró un álbum único para preparar.')
    manifest = matches[0]
    result = dict(ready=0, vetoed=0, unsupported=0, failed=0)
    for photo in gallery.photos(manifest):
        try:
            data = gallery.media.thumbnail(manifest, photo)
            result['vetoed' if data is None else 'ready'] += 1
        except UnsupportedPreview:
            result['unsupported'] += 1
        except Exception:
            result['failed'] += 1
    result['ok'] = result['failed'] == 0 and result['unsupported'] == 0
    return result


def main():
    parser = argparse.ArgumentParser(description='Preparar miniaturas persistentes antes de compartir un álbum.')
    parser.add_argument('--slug', required=True, help='Identificador del álbum, sin enlace ni clave de acceso.')
    args = parser.parse_args()
    # Uses the album service's existing environment. Never print credentials or access URLs.
    from album_app import create_app
    result = prepare(create_app().extensions['album_gallery'], args.slug)
    print(json.dumps(result))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
