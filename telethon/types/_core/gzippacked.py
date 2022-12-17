import gzip
import struct

from ..._misc import tlobject


class GzipPacked(tlobject.TLObject):
    CONSTRUCTOR_ID = 0x3072cfa1

    def __init__(self, data):
        self.data = data

    @staticmethod
    def gzip_if_smaller(content_related, data):
        """Calls bytes(request), and based on a certain threshold,
           optionally gzips the resulting data. If the gzipped data is
           smaller than the original byte array, this is returned instead.

           Note that this only applies to content related requests.
        """
        if not content_related or len(data) <= 512:
            return data
        gzipped = bytes(GzipPacked(data))
        return gzipped if len(gzipped) < len(data) else data

    def __bytes__(self):
        return struct.pack('<I', GzipPacked.CONSTRUCTOR_ID) + \
               tlobject.TLObject._serialize_bytes(gzip.compress(self.data))

    @staticmethod
    def read(reader):
        constructor = reader.read_int(signed=False)
        assert constructor == GzipPacked.CONSTRUCTOR_ID
        return gzip.decompress(reader.tgread_bytes())

    @classmethod
    def _from_reader(cls, reader):
        return GzipPacked(gzip.decompress(reader.tgread_bytes()))

    def to_dict(self):
        return {
            '_': 'GzipPacked',
            'data': self.data
        }
