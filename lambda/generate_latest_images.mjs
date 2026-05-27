import { S3Client, ListObjectsV2Command, PutObjectCommand } from "@aws-sdk/client-s3";

const REGION = "us-east-2";
const SOURCE_BUCKET = "gorgewhitewater";
const PREFIX = "tactacam/Husum/";
const SITE_BUCKET = "gorgegauge.com";
const OUTPUT_KEY = "latest.json";

const PUBLIC_URL_PREFIX = `https://${SOURCE_BUCKET}.s3.${REGION}.amazonaws.com/`;

const s3 = new S3Client({ region: REGION });

export const handler = async () => {
  const data = await s3.send(
    new ListObjectsV2Command({
      Bucket: SOURCE_BUCKET,
      Prefix: PREFIX
    })
  );

  const items = (data.Contents || [])
    .filter((obj) => obj.Key && !obj.Key.endsWith("/"))
    .sort((a, b) => new Date(b.LastModified) - new Date(a.LastModified))
    .slice(0, 10)
    .map((obj) => {
      const encodedKey = encodeURIComponent(obj.Key).replace(/%2F/g, "/");
      return {
        fileName: obj.Key.startsWith(PREFIX) ? obj.Key.substring(PREFIX.length) : obj.Key,
        url: PUBLIC_URL_PREFIX + encodedKey,
        lastModified: obj.LastModified
      };
    });

  await s3.send(
    new PutObjectCommand({
      Bucket: SITE_BUCKET,
      Key: OUTPUT_KEY,
      Body: JSON.stringify(items, null, 2),
      ContentType: "application/json",
      CacheControl: "no-store"
    })
  );

  return { ok: true, count: items.length };
};
