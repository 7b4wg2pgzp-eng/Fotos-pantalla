import struct
import unittest
from unittest.mock import MagicMock
from album_dng import extract_preview
from album_media import AlbumMedia, AlbumUnavailable


def fixture():
    preview = b'\xff\xd8embedded-preview\xff\xd9'
    tags = [(254,1),(256,8064),(257,6048),(259,7),(273,256),(279,len(preview))]
    header = b'II' + struct.pack('<HI',42,8)
    directory = struct.pack('<H',len(tags)) + b''.join(struct.pack('<HHII',tag,4,1,value) for tag,value in tags) + b'\0'*4
    return (header+directory).ljust(256,b'\0')+preview, preview


class DngPreviewTests(unittest.TestCase):
    def test_embedded_preview_without_reading_large_raw_image(self):
        blob,preview=fixture(); reads=[]
        def read(offset,length):
            reads.append(length)
            return blob[offset:offset+length]
        self.assertEqual(extract_preview(read,90_000_000),preview)
        self.assertLess(sum(reads),1024)

    def test_rejects_offsets_beyond_file_without_unbounded_read(self):
        blob,_=fixture()
        damaged=bytearray(blob)
        # Strip offset points outside the actual file.
        struct.pack_into('<I',damaged,8+2+4*12+8,0xffffffff)
        with self.assertRaises(ValueError):
            extract_preview(lambda o,n:bytes(damaged[o:o+n]),len(damaged))
        with self.assertRaises(ValueError):
            extract_preview(lambda o,n:b'BAD!',100)

    def test_drive_ranges_and_changed_source_are_verified(self):
        blob,preview=fixture(); service=MagicMock(); files=service.files()
        photo={'id':'dng','size':'90000000','md5Checksum':'verified-version'}
        files.get().execute.return_value=photo
        ranges=[]
        class Request:
            def __init__(self): self.headers={}
            def execute(self,num_retries=0):
                a,b=map(int,self.headers['Range'].removeprefix('bytes=').split('-'))
                ranges.append((a,b))
                return blob[a:b+1].ljust(b-a+1,b'\0')
        files.get_media.side_effect=lambda **kw:Request()
        media=AlbumMedia(MagicMock(service=service))
        self.assertEqual(media.dng_preview(photo),preview)
        self.assertLess(sum(b-a+1 for a,b in ranges),200000)
        files.get().execute.side_effect=[photo,dict(photo,md5Checksum='changed')]
        with self.assertRaises(AlbumUnavailable):media.dng_preview(photo)
