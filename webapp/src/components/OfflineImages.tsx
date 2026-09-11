import { useEffect, useState } from "react";
import { offlineApi, type OfflineBody } from "../domain/offlineReading";

function LocalImage({ itemKey, body, image }: { itemKey: string; body: OfflineBody; image: OfflineBody["images"][number] }) {
  const [url, setUrl] = useState("");
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    const abort = new AbortController(); let objectUrl = "";
    setUrl(""); setFailed(false);
    if (image.state === "verified") void offlineApi.image(itemKey, body.job_id, image.index, abort.signal).then((blob) => {
      if (abort.signal.aborted) return;
      objectUrl = URL.createObjectURL(blob); setUrl(objectUrl);
    }).catch(() => { if (!abort.signal.aborted) setFailed(true); });
    return () => { abort.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [itemKey, body.job_id, image.index, image.state]);
  return <figure>{image.state !== "verified" || failed ? <p>Image unavailable in this offline copy{image.alt ? `: ${image.alt}` : "."}</p>
    : url ? <img src={url} alt={image.alt} onError={() => setFailed(true)} style={{ maxWidth: "100%" }} /> : <p role="status">Loading saved image…</p>}</figure>;
}
export default function OfflineImages({ body, itemKey }: { body: OfflineBody; itemKey: string }) {
  return <>{body.images_omitted ? <p>Some images were omitted to stay within the download limit.</p> : null}
    {body.images.map((image) => <LocalImage key={`${body.job_id}:${image.index}`} body={body} image={image} itemKey={itemKey} />)}</>;
}
