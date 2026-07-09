import { useCallback, useEffect, useState } from "react"
import { toast } from "sonner"
import {
  GlobeIcon,
  PencilIcon,
  PlusIcon,
  RefreshCwIcon,
  Trash2Icon,
} from "lucide-react"

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
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { ScrollArea } from "@/components/ui/scroll-area"
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
import { Textarea } from "@/components/ui/textarea"

// The declared-domain catalog: first-class domain entities with a description
// and context. Domains must be declared here before Glue databases can be
// mapped into them (see MappingsView).
export default function DomainsView({ api, onChanged }) {
  const [declaredDomains, setDeclaredDomains] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const load = useCallback(async () => {
    if (!api) return
    setLoading(true)
    setError(null)
    try {
      const declared = await api.listDeclaredDomains()
      setDeclaredDomains(Array.isArray(declared) ? declared : [])
    } catch (e) {
      setError(e.message || String(e))
    } finally {
      setLoading(false)
    }
  }, [api])

  useEffect(() => {
    load()
  }, [load])

  const refresh = useCallback(async () => {
    await load()
    onChanged?.()
  }, [load, onChanged])

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader className="border-b">
          <CardTitle className="flex items-center gap-2">
            <GlobeIcon className="size-4" />
            Data domains
          </CardTitle>
          <CardDescription>
            Declare domains before mapping Glue databases into them. Each domain
            represents a business area with a description and context.
          </CardDescription>
          <div className="col-start-2 row-span-2 row-start-1 flex items-center gap-2 self-start justify-self-end">
            <Button variant="outline" onClick={refresh} disabled={loading}>
              {loading ? <Spinner /> : <RefreshCwIcon data-icon="inline-start" />}
              Refresh
            </Button>
            <NewDomainDialog api={api} onCreated={refresh} />
          </div>
        </CardHeader>
        <CardContent>
          {error ? (
            <Alert variant="destructive">
              <AlertTitle>Failed to load domains</AlertTitle>
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          ) : loading ? (
            <div className="flex flex-col gap-2">
              <Skeleton className="h-8 w-full" />
              <Skeleton className="h-8 w-full" />
            </div>
          ) : declaredDomains.length === 0 ? (
            <Alert>
              <GlobeIcon />
              <AlertTitle>No domains declared</AlertTitle>
              <AlertDescription>
                Create a domain with the "New domain" button to define a business
                area. Then map Glue databases into it from the Mappings page.
              </AlertDescription>
            </Alert>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Domain</TableHead>
                  <TableHead>Description</TableHead>
                  <TableHead>Created</TableHead>
                  <TableHead className="w-0" />
                </TableRow>
              </TableHeader>
              <TableBody>
                {declaredDomains.map((d) => (
                  <TableRow key={d.data_domain}>
                    <TableCell className="font-medium">
                      <DomainDetailsDialog domain={d} />
                    </TableCell>
                    <TableCell className="max-w-xs truncate text-muted-foreground">
                      {d.description || "—"}
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      {d.created_at
                        ? new Date(d.created_at).toLocaleString()
                        : "—"}
                    </TableCell>
                    <TableCell>
                      <div className="flex items-center justify-end gap-1">
                        <EditDomainDialog
                          api={api}
                          domain={d}
                          onSaved={refresh}
                        />
                        <DeleteDomainDialog
                          api={api}
                          domain={d}
                          onDeleted={refresh}
                        />
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

// -- Declared-domain details dialog -----------------------------------------

// Read-only view of a domain's full declaration. Opened by clicking the domain
// name in the table. Renders straight from the row (listDeclaredDomains already
// returns description, context, created_at, updated_at) — no extra fetch.
function DomainDetailsDialog({ domain }) {
  const fmt = (ts) => (ts ? new Date(ts).toLocaleString() : "—")

  return (
    <Dialog>
      <DialogTrigger asChild>
        <button
          type="button"
          className="text-left font-medium text-foreground underline-offset-4 hover:underline"
        >
          {domain.data_domain}
        </button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <GlobeIcon className="size-4" />
            {domain.data_domain}
          </DialogTitle>
          <DialogDescription className="break-words">
            {domain.description || "No description provided."}
          </DialogDescription>
        </DialogHeader>
        <div className="flex min-w-0 flex-col gap-4">
          <div className="flex min-w-0 flex-col gap-1.5">
            <Label className="text-muted-foreground">Context</Label>
            {domain.context ? (
              <ScrollArea className="max-h-64 w-full rounded-md border p-3">
                <p className="text-sm break-words whitespace-pre-wrap">
                  {domain.context}
                </p>
              </ScrollArea>
            ) : (
              <p className="text-sm text-muted-foreground italic">
                No additional context.
              </p>
            )}
          </div>
          <div className="grid grid-cols-2 gap-4 text-sm">
            <div className="flex flex-col gap-1">
              <Label className="text-muted-foreground">Created</Label>
              <span>{fmt(domain.created_at)}</span>
            </div>
            <div className="flex flex-col gap-1">
              <Label className="text-muted-foreground">Updated</Label>
              <span>{fmt(domain.updated_at)}</span>
            </div>
          </div>
        </div>
        <DialogFooter>
          <DialogClose asChild>
            <Button type="button" variant="outline">
              Close
            </Button>
          </DialogClose>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// -- Declared-domain create dialog ------------------------------------------

function NewDomainDialog({ api, onCreated }) {
  const [open, setOpen] = useState(false)
  const [name, setName] = useState("")
  const [description, setDescription] = useState("")
  const [context, setContext] = useState("")
  const [submitting, setSubmitting] = useState(false)

  const reset = () => {
    setName("")
    setDescription("")
    setContext("")
  }

  const submit = async (e) => {
    e.preventDefault()
    if (!name) {
      toast.error("Enter a domain name.")
      return
    }
    setSubmitting(true)
    try {
      await api.declareDomain(name, description, context)
      toast.success(`Domain "${name}" declared`)
      setOpen(false)
      reset()
      onCreated?.()
    } catch (err) {
      toast.error(`Could not declare domain: ${err.message || err}`)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button>
          <PlusIcon data-icon="inline-start" />
          New domain
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-lg">
        <form onSubmit={submit} className="flex flex-col gap-4">
          <DialogHeader>
            <DialogTitle>Declare a new domain</DialogTitle>
            <DialogDescription>
              A domain groups related Glue databases under a shared business
              context. Provide a short description and optional richer context
              (used by the harvester and exposed to agents over MCP).
            </DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-4">
            <div className="flex flex-col gap-2">
              <Label htmlFor="new-domain-name">Domain name</Label>
              <Input
                id="new-domain-name"
                value={name}
                placeholder="e.g. sales"
                onChange={(e) => setName(e.target.value.trim().toLowerCase())}
              />
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor="new-domain-desc">Description</Label>
              <Input
                id="new-domain-desc"
                value={description}
                placeholder="Short one-liner (e.g. 'Revenue & order pipelines')"
                onChange={(e) => setDescription(e.target.value)}
              />
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor="new-domain-ctx">Context (optional)</Label>
              <Textarea
                id="new-domain-ctx"
                value={context}
                placeholder="Richer context: what this domain covers, what it excludes, key stakeholders, etc."
                onChange={(e) => setContext(e.target.value)}
                rows={4}
              />
            </div>
          </div>
          <DialogFooter>
            <DialogClose asChild>
              <Button type="button" variant="outline">
                Cancel
              </Button>
            </DialogClose>
            <Button type="submit" disabled={submitting}>
              {submitting ? <Spinner /> : null}
              Create domain
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

// -- Declared-domain edit dialog --------------------------------------------

// Edits a domain's description and context. The domain name is the registry
// partition key and is immutable, so it's shown read-only. Saving PUTs to
// /domain-defs/{domain} (an upsert): created_at is preserved, updated_at is
// bumped, and the domain concept doc is re-materialised for semantic search.
function EditDomainDialog({ api, domain, onSaved }) {
  const [open, setOpen] = useState(false)
  const [description, setDescription] = useState(domain.description || "")
  const [context, setContext] = useState(domain.context || "")
  const [submitting, setSubmitting] = useState(false)

  // Re-seed the form from the latest domain whenever the dialog is (re)opened,
  // so a refresh elsewhere doesn't leave stale text in a closed dialog.
  useEffect(() => {
    if (open) {
      setDescription(domain.description || "")
      setContext(domain.context || "")
    }
  }, [open, domain.description, domain.context])

  const submit = async (e) => {
    e.preventDefault()
    setSubmitting(true)
    try {
      await api.updateDomain(domain.data_domain, description, context)
      toast.success(`Domain "${domain.data_domain}" updated`)
      setOpen(false)
      onSaved?.()
    } catch (err) {
      toast.error(`Could not update domain: ${err.message || err}`)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="ghost" size="icon-sm" aria-label="Edit domain">
          <PencilIcon />
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-lg">
        <form onSubmit={submit} className="flex flex-col gap-4">
          <DialogHeader>
            <DialogTitle>Edit domain</DialogTitle>
            <DialogDescription>
              Update the description and context for{" "}
              <span className="font-medium text-foreground">
                {domain.data_domain}
              </span>
              . Changes are re-exposed to the harvester and to agents over MCP.
            </DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-4">
            <div className="flex flex-col gap-2">
              <Label htmlFor="edit-domain-name">Domain name</Label>
              <Input
                id="edit-domain-name"
                value={domain.data_domain}
                disabled
                readOnly
              />
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor="edit-domain-desc">Description</Label>
              <Input
                id="edit-domain-desc"
                value={description}
                placeholder="Short one-liner (e.g. 'Revenue & order pipelines')"
                onChange={(e) => setDescription(e.target.value)}
              />
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor="edit-domain-ctx">Context (optional)</Label>
              <Textarea
                id="edit-domain-ctx"
                value={context}
                placeholder="Richer context: what this domain covers, what it excludes, key stakeholders, etc."
                onChange={(e) => setContext(e.target.value)}
                rows={4}
              />
            </div>
          </div>
          <DialogFooter>
            <DialogClose asChild>
              <Button type="button" variant="outline">
                Cancel
              </Button>
            </DialogClose>
            <Button type="submit" disabled={submitting}>
              {submitting ? <Spinner /> : null}
              Save changes
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

// -- Declared-domain delete dialog ------------------------------------------

function DeleteDomainDialog({ api, domain, onDeleted }) {
  const [open, setOpen] = useState(false)
  const [deleting, setDeleting] = useState(false)

  const remove = async () => {
    setDeleting(true)
    try {
      await api.deleteDeclaredDomain(domain.data_domain)
      toast.success(`Domain "${domain.data_domain}" deleted`)
      setOpen(false)
      onDeleted?.()
    } catch (err) {
      toast.error(`Could not delete domain: ${err.message || err}`)
    } finally {
      setDeleting(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="ghost" size="icon-sm" aria-label="Delete domain">
          <Trash2Icon />
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Delete domain?</DialogTitle>
          <DialogDescription>
            This deletes the domain declaration for{" "}
            <span className="font-medium text-foreground">
              {domain.data_domain}
            </span>
            . All dataset mappings under this domain must be removed first.
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
