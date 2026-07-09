import { useCallback, useEffect, useRef, useState } from "react"
import { toast } from "sonner"
import {
  FileTextIcon,
  RefreshCwIcon,
  Trash2Icon,
  UploadIcon,
} from "lucide-react"

import { uploadToPresigned } from "@/lib/api"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"

function formatBytes(bytes) {
  if (bytes == null || Number.isNaN(bytes)) return "—"
  if (bytes === 0) return "0 B"
  const units = ["B", "KB", "MB", "GB"]
  const i = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)))
  return `${(bytes / 1024 ** i).toFixed(i === 0 ? 0 : 1)} ${units[i]}`
}

// Upload / list / delete the source docs under a dataset's .context/ prefix.
// These are persisted so incremental harvests can reuse them.
export default function ContextView({ api, selection }) {
  const [docs, setDocs] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [uploading, setUploading] = useState(false)
  const fileInputRef = useRef(null)

  const domain = selection?.data_domain
  const dataset = selection?.dataset
  const hasSelection = Boolean(domain && dataset)

  const load = useCallback(async () => {
    if (!api || !hasSelection) return
    setLoading(true)
    setError(null)
    try {
      const list = await api.listContext(domain, dataset)
      setDocs(Array.isArray(list) ? list : [])
    } catch (e) {
      setError(e.message || String(e))
    } finally {
      setLoading(false)
    }
  }, [api, domain, dataset, hasSelection])

  useEffect(() => {
    setDocs([])
    load()
  }, [load])

  const onPickFile = async (e) => {
    const file = e.target.files?.[0]
    // Reset the input so picking the same file again re-triggers change.
    if (fileInputRef.current) fileInputRef.current.value = ""
    if (!file || !hasSelection) return

    setUploading(true)
    const contentType = file.type || "application/octet-stream"
    try {
      const { url, fields, max_bytes } = await api.presignUpload(
        domain,
        dataset,
        file.name,
        contentType
      )
      if (!url || !fields) throw new Error("presign response missing 'url'/'fields'")
      // Friendly client-side pre-check; S3 enforces the same cap server-side.
      if (max_bytes && file.size > max_bytes) {
        throw new Error(
          `file is ${(file.size / 1048576).toFixed(1)} MB; max is ${(
            max_bytes / 1048576
          ).toFixed(0)} MB`
        )
      }
      await uploadToPresigned({ url, fields }, file)
      toast.success(`Uploaded ${file.name}`)
      await load()
    } catch (err) {
      toast.error(`Upload failed: ${err.message || err}`)
    } finally {
      setUploading(false)
    }
  }

  if (!hasSelection) {
    return (
      <Alert>
        <FileTextIcon />
        <AlertTitle>Select a dataset first</AlertTitle>
        <AlertDescription>
          Pick a dataset from the sidebar to manage its context documents.
        </AlertDescription>
      </Alert>
    )
  }

  return (
    <Card>
      <CardHeader className="border-b">
        <CardTitle className="flex items-center gap-2">
          <FileTextIcon className="size-4" />
          Context docs
        </CardTitle>
        <CardDescription>
          Source docs for{" "}
          <span className="font-medium text-foreground">
            {domain}/{dataset}
          </span>{" "}
        </CardDescription>
        <div className="col-start-2 row-span-2 row-start-1 flex items-center gap-2 self-start justify-self-end">
          <Button variant="outline" onClick={load} disabled={loading}>
            {loading ? <Spinner /> : <RefreshCwIcon data-icon="inline-start" />}
            Refresh
          </Button>
          <input
            ref={fileInputRef}
            type="file"
            className="hidden"
            accept=".pdf,.docx,.pptx,.xlsx,.xml,.md,.txt,.csv"
            onChange={onPickFile}
          />
          <Button
            onClick={() => fileInputRef.current?.click()}
            disabled={uploading}
          >
            {uploading ? <Spinner /> : <UploadIcon data-icon="inline-start" />}
            Upload
          </Button>
        </div>
      </CardHeader>
      <CardContent>
        {error ? (
          <Alert variant="destructive">
            <AlertTitle>Failed to load context docs</AlertTitle>
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        ) : loading ? (
          <div className="flex flex-col gap-2">
            <Skeleton className="h-8 w-full" />
            <Skeleton className="h-8 w-full" />
          </div>
        ) : docs.length === 0 ? (
          <Alert>
            <UploadIcon />
            <AlertTitle>No context docs</AlertTitle>
            <AlertDescription>
              Upload PDF, Word, PowerPoint, Excel, XML, Markdown, text or CSV
              source docs to enrich the harvest for this dataset.
            </AlertDescription>
          </Alert>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Filename</TableHead>
                <TableHead className="w-32">Size</TableHead>
                <TableHead className="w-0" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {docs.map((doc) => (
                <TableRow key={doc.filename}>
                  <TableCell className="font-medium">{doc.filename}</TableCell>
                  <TableCell className="text-muted-foreground">
                    {formatBytes(doc.size)}
                  </TableCell>
                  <TableCell>
                    <DeleteDocDialog
                      api={api}
                      domain={domain}
                      dataset={dataset}
                      filename={doc.filename}
                      onDeleted={load}
                    />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  )
}

function DeleteDocDialog({ api, domain, dataset, filename, onDeleted }) {
  const [open, setOpen] = useState(false)
  const [deleting, setDeleting] = useState(false)

  const remove = async () => {
    setDeleting(true)
    try {
      await api.deleteContext(domain, dataset, filename)
      toast.success(`Deleted ${filename}`)
      setOpen(false)
      onDeleted?.()
    } catch (err) {
      toast.error(`Could not delete: ${err.message || err}`)
    } finally {
      setDeleting(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="ghost" size="icon-sm" aria-label="Delete document">
          <Trash2Icon />
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Delete document?</DialogTitle>
          <DialogDescription>
            Remove{" "}
            <span className="font-medium text-foreground">{filename}</span> from{" "}
            {domain}/{dataset} context docs.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <DialogClose asChild>
            <Button type="button" variant="outline">
              Cancel
            </Button>
          </DialogClose>
          <Button variant="destructive" onClick={remove} disabled={deleting}>
            {deleting ? <Spinner /> : <Trash2Icon data-icon="inline-start" />}
            Delete
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
