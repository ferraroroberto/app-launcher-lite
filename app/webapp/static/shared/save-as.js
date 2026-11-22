/* Hand the browser a file to save under its own name. octet-stream, so
 * Safari saves the blob instead of rendering a text blob inline. `data` is
 * text or a Blob.
 */
export function saveAs(name, data) {
  const url = URL.createObjectURL(new Blob([data], { type: 'application/octet-stream' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  a.hidden = true;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(function () { URL.revokeObjectURL(url); }, 4000);
}
