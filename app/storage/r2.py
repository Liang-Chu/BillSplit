from app.storage.base import StorageBackend


class R2Storage(StorageBackend):
    """Cloudflare R2 through its S3-compatible API."""

    name = "r2"

    def __init__(self, endpoint_url: str, access_key_id: str, secret_access_key: str, bucket: str, presign_seconds: int):
        import boto3
        from botocore.config import Config

        self.bucket = bucket
        self.presign_seconds = presign_seconds
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name="auto",
            config=Config(signature_version="s3v4", retries={"max_attempts": 2}, connect_timeout=5, read_timeout=30),
        )

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)

    def get(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def presigned_url(self, key: str, filename: str | None = None) -> str | None:
        params = {"Bucket": self.bucket, "Key": key}
        if filename:
            params["ResponseContentDisposition"] = f'inline; filename="{filename}"'
        return self.client.generate_presigned_url("get_object", Params=params, ExpiresIn=self.presign_seconds)
