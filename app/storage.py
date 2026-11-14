"""Private blob store. Keys are generated from UUIDs, never from uploaded names."""
import os
import re
import shutil
from functools import lru_cache
from pathlib import Path, PureWindowsPath
from tempfile import NamedTemporaryFile

import boto3
from botocore.config import Config
from app.config import get_settings

ALLOWED_EXTENSIONS = {'.pdf', '.docx', '.txt', '.md'}


def safe_filename(name: str | None) -> str:
    base = Path(PureWindowsPath(name or '').name).name
    base = re.sub(r'[\x00-\x1f\x7f]', '', base).strip(' .')
    return (base or 'document')[:255]


def source_key(tenant_id: str, document_id: str, version_id: str, suffix: str) -> str:
    return f'tenants/{tenant_id}/documents/{document_id}/versions/{version_id}/source{suffix}'


def index_prefix(tenant_id: str, document_id: str, version_id: str) -> str:
    return f'tenants/{tenant_id}/documents/{document_id}/versions/{version_id}/index'


def document_prefix(tenant_id: str, document_id: str) -> str:
    return f'tenants/{tenant_id}/documents/{document_id}/'


class LocalStore:
    def __init__(self, root: Path):
        self.root = root / 'objects'
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, key: str) -> Path:
        if not key or key.startswith('/') or '..' in Path(key).parts:
            raise ValueError('Invalid object key')
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError('Invalid object key')
        return path

    def put_file(self, path: Path, key: str):
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + '.' + os.urandom(8).hex())
        try:
            shutil.copyfile(path, tmp)
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)

    def get_file(self, key: str, path: Path):
        shutil.copyfile(self.path(key), path)

    def delete_prefix(self, prefix: str):
        if not prefix.startswith('tenants/') or len(prefix.split('/')) < 3:
            raise ValueError('Refusing broad delete')
        path = self.path(prefix.rstrip('/'))
        if path.is_file():
            path.unlink(missing_ok=True)
        else:
            shutil.rmtree(path, ignore_errors=True)

    def ping(self):
        return self.root.is_dir()


class S3Store:
    def __init__(self):
        s = get_settings()
        self.bucket = s.s3_bucket
        kwargs = {'region_name': s.s3_region,
                  'config': Config(retries={'max_attempts': 5, 'mode': 'standard'},
                                   s3={'addressing_style': 'path'})}
        if s.s3_endpoint_url:
            kwargs['endpoint_url'] = s.s3_endpoint_url
        if s.s3_access_key_id:
            kwargs['aws_access_key_id'] = s.s3_access_key_id
            kwargs['aws_secret_access_key'] = s.s3_secret_access_key
        self.client = boto3.client('s3', **kwargs)

    def put_file(self, path: Path, key: str):
        self.client.upload_file(str(path), self.bucket, key, ExtraArgs={'ServerSideEncryption': 'AES256'})

    def get_file(self, key: str, path: Path):
        self.client.download_file(self.bucket, key, str(path))

    def delete_prefix(self, prefix: str):
        if not prefix.startswith('tenants/') or len(prefix.split('/')) < 3:
            raise ValueError('Refusing broad delete')
        paginator = self.client.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            keys = [{'Key': obj['Key']} for obj in page.get('Contents', [])]
            for start in range(0, len(keys), 1000):
                result = self.client.delete_objects(Bucket=self.bucket,
                                                    Delete={'Objects': keys[start:start + 1000], 'Quiet': True})
                if result.get('Errors'):
                    raise RuntimeError('Object-store deletion failed')

    def ping(self):
        self.client.head_bucket(Bucket=self.bucket)
        return True


@lru_cache
def get_store():
    s = get_settings()
    return S3Store() if s.storage_backend == 's3' else LocalStore(s.data_dir)


def create_dirs():
    s = get_settings()
    for sub in ['tmp', 'cache']:
        (s.data_dir / sub).mkdir(parents=True, exist_ok=True)


def scan_file(path: Path):
    """clamd INSTREAM; errors fail closed to prevent unscanned uploads."""
    s = get_settings()
    if not s.clamav_enabled:
        return
    import socket
    import struct
    with socket.create_connection((s.clamav_host, s.clamav_port), timeout=30) as sock:
        sock.settimeout(120)
        sock.sendall(b'zINSTREAM\x00')
        with path.open('rb') as stream:
            while chunk := stream.read(1024 * 1024):
                sock.sendall(struct.pack('!I', len(chunk)) + chunk)
        sock.sendall(struct.pack('!I', 0))
        response = b''
        while not response.endswith(b'\x00') and len(response) < 8192:
            part = sock.recv(1024)
            if not part:
                break
            response += part
    if not response.endswith(b'OK\x00'):
        raise ValueError('Malware scanning failed or file was flagged')
