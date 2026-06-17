from app.destinations import get_backend
from app.destinations.local import LocalBackend
from app.destinations.rsync import RsyncBackend
from app.destinations.sftp import SFTPBackend
from app.models import Destination


def test_destination_type_backend_mapping():
    assert isinstance(get_backend(Destination(type="local", name="local")), LocalBackend)
    assert isinstance(get_backend(Destination(type="nfs", name="nfs")), LocalBackend)
    assert isinstance(get_backend(Destination(type="smb", name="smb")), LocalBackend)
    assert isinstance(get_backend(Destination(type="sftp", name="sftp")), SFTPBackend)
    assert isinstance(get_backend(Destination(type="rsync", name="rsync")), RsyncBackend)
