// Builds the "latest ten" gallery feeds the site reads: latestHusum.json and
// latestTroutCreek.json in the site bucket. Mirrors the deployed Lambda as of
// 2026-11-14.
//
// Every camera's uploads land under the same prefix (the scraper files them
// all as tactacam/Husum/), so each page's feed is selected by camera IMEI,
// the leading field of the filename. Husum has two cameras since 2026-11-14:
// the original and the Truss.
import { S3Client, ListObjectsV2Command, PutObjectCommand } from "@aws-sdk/client-s3";

const REGION = "us-east-2";
const SOURCE_BUCKET = "gorgewhitewater";
const SITE_BUCKET = "gorgegauge.com";

const LIST_PREFIX = "tactacam/";
const PUBLIC_URL_PREFIX = `https://${SOURCE_BUCKET}.s3.${REGION}.amazonaws.com/`;

const s3 = new S3Client({ region: REGION });

const CAMERAS = [
  {
    name: "Husum",
    cameraIdPrefixes: ["865509053179515", "016578004123334"],   // original, Truss
    outputKey: "latestHusum.json"
  },
  {
    name: "TroutCreek",
    cameraIdPrefixes: ["016578000423746"],
    outputKey: "latestTroutCreek.json"
  }
];

function fileNameFromKey(key) {
  const parts = key.split("/");
  return parts[parts.length - 1] || key;
}

async function listAllObjectsUnderPrefix(prefix) {
  const all = [];
  let token;

  do {
    const resp = await s3.send(
      new ListObjectsV2Command({
        Bucket: SOURCE_BUCKET,
        Prefix: prefix,
        ContinuationToken: token
      })
    );

    if (resp.Contents && resp.Contents.length) all.push(...resp.Contents);
    token = resp.IsTruncated ? resp.NextContinuationToken : undefined;
  } while (token);

  return all;
}

function buildLatest(items, cameraIdPrefixes) {
  return items
    .filter((obj) => obj.Key && !obj.Key.endsWith("/"))
    .filter((obj) => {
      const file = fileNameFromKey(obj.Key || "");
      return cameraIdPrefixes.some((p) => file.startsWith(p));
    })
    .sort((a, b) => new Date(b.LastModified).getTime() - new Date(a.LastModified).getTime())
    .slice(0, 10)
    .map((obj) => {
      const key = obj.Key || "";
      const encodedKey = encodeURIComponent(key).replace(/%2F/g, "/");
      return {
        fileName: fileNameFromKey(key),
        url: PUBLIC_URL_PREFIX + encodedKey,
        lastModified: obj.LastModified
      };
    });
}

async function writeJson(outputKey, items) {
  await s3.send(
    new PutObjectCommand({
      Bucket: SITE_BUCKET,
      Key: outputKey,
      Body: JSON.stringify(items, null, 2),
      ContentType: "application/json",
      CacheControl: "no-store"
    })
  );
}

export const handler = async () => {
  const allObjects = await listAllObjectsUnderPrefix(LIST_PREFIX);

  const results = [];

  for (const cam of CAMERAS) {
    const latest = buildLatest(allObjects, cam.cameraIdPrefixes);
    await writeJson(cam.outputKey, latest);

    results.push({ name: cam.name, outputKey: cam.outputKey, count: latest.length });
  }

  return { ok: true, results };
};
