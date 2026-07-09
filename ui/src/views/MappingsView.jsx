import { useCallback, useEffect, useState } from "react"
import { toast } from "sonner"
import { DatabaseIcon, PlusIcon, RefreshCwIcon, Trash2Icon } from "lucide-react"

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
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
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

// Source types a mapping can draw from. Only "glue" is supported today; this
// list is the single place the UI knows about sources, so adding a type later
// (Redshift, BigQuery, …) is a one-line change here + backend support. Mirrors
// okf_core.sources.SUPPORTED_SOURCE_TYPES.
const SOURCE_TYPES = [{ value: "glue", label: "AWS Glue" }]
const DEFAULT_SOURCE_TYPE = "glue"

// Human label for a source type (falls back to the raw value / a dash).
function sourceLabel(type) {
  if (!type) return "—"
  return SOURCE_TYPES.find((s) => s.value === type)?.label || type
}

// Maps Glue databases into declared data domains. Loads the existing registry
// mappings, the list of Glue databases, and the declared-domain catalog (which
// populates the domain picker — a mapping must select a pre-declared domain).
export default function MappingsView({ api, onChanged }) {
  const [databases, setDatabases] = useState([])
  const [domains, setDomains] = useState([])
  const [declaredDomains, setDeclaredDomains] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const load = useCallback(async () => {
    if (!api) return
    setLoading(true)
    setError(null)
    try {
      const [dbs, doms, declared] = await Promise.all([
        api.listGlueDatabases(),
        api.listDomains(),
        api.listDeclaredDomains(),
      ])
      setDatabases(Array.isArray(dbs) ? dbs : [])
      setDomains(Array.isArray(doms) ? doms : [])
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
            <DatabaseIcon className="size-4" />
            Dataset mappings
          </CardTitle>
          <CardDescription>
            Map a Glue database into a declared domain to start harvesting it.
          </CardDescription>
          <div className="col-start-2 row-span-2 row-start-1 flex items-center gap-2 self-start justify-self-end">
            <Button variant="outline" onClick={refresh} disabled={loading}>
              {loading ? <Spinner /> : <RefreshCwIcon data-icon="inline-start" />}
              Refresh
            </Button>
            <NewMappingDialog
              api={api}
              databases={databases}
              declaredDomains={declaredDomains}
              onCreated={refresh}
            />
          </div>
        </CardHeader>
        <CardContent>
          {error ? (
            <Alert variant="destructive">
              <AlertTitle>Failed to load mappings</AlertTitle>
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          ) : loading ? (
            <div className="flex flex-col gap-2">
              <Skeleton className="h-8 w-full" />
              <Skeleton className="h-8 w-full" />
              <Skeleton className="h-8 w-full" />
            </div>
          ) : domains.length === 0 ? (
            <Alert>
              <DatabaseIcon />
              <AlertTitle>No dataset mappings yet</AlertTitle>
              <AlertDescription>
                {declaredDomains.length === 0
                  ? "Declare a domain first (Domains), then create a mapping here to start harvesting a Glue database."
                  : 'Create one with the "New mapping" button to start harvesting a Glue database.'}
              </AlertDescription>
            </Alert>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Data domain</TableHead>
                  <TableHead>Dataset</TableHead>
                  <TableHead>Source</TableHead>
                  <TableHead>Glue database</TableHead>
                  <TableHead>Created</TableHead>
                  <TableHead className="w-0" />
                </TableRow>
              </TableHeader>
              <TableBody>
                {domains.map((d) => (
                  <TableRow key={`${d.data_domain}/${d.dataset}`}>
                    <TableCell className="font-medium">
                      {d.data_domain}
                    </TableCell>
                    <TableCell>{d.dataset}</TableCell>
                    <TableCell>{sourceLabel(d.source?.type)}</TableCell>
                    <TableCell className="font-mono text-xs">
                      {d.source?.glue_database || d.glue_database}
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      {d.created_at
                        ? new Date(d.created_at).toLocaleString()
                        : "—"}
                    </TableCell>
                    <TableCell>
                      <DeleteMappingDialog
                        api={api}
                        mapping={d}
                        onDeleted={refresh}
                      />
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

// -- Mapping create dialog (domain is a Select over declared domains) -------

function NewMappingDialog({ api, databases, declaredDomains, onCreated }) {
  const [open, setOpen] = useState(false)
  const [domain, setDomain] = useState("")
  // Source type is fixed to the default (glue) for now — the picker is present
  // and read-only so the concept is visible and the form is ready for more
  // source types without a redesign.
  const [sourceType] = useState(DEFAULT_SOURCE_TYPE)
  const [glueDatabase, setGlueDatabase] = useState("")
  const [submitting, setSubmitting] = useState(false)

  // The dataset name is NOT user-supplied: the harvest runtime queries Glue by
  // the dataset name directly, so the dataset must equal the Glue database name.
  const dataset = glueDatabase

  const reset = () => {
    setDomain("")
    setGlueDatabase("")
  }

  const submit = async (e) => {
    e.preventDefault()
    if (!domain || !glueDatabase) {
      toast.error("Select both a domain and a Glue database.")
      return
    }
    setSubmitting(true)
    try {
      await api.setDomainMapping(domain, dataset, glueDatabase, sourceType)
      toast.success(`Mapped ${domain}/${dataset} -> ${glueDatabase}`)
      setOpen(false)
      reset()
      onCreated?.()
    } catch (err) {
      toast.error(`Could not save mapping: ${err.message || err}`)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button>
          <PlusIcon data-icon="inline-start" />
          New mapping
        </Button>
      </DialogTrigger>
      <DialogContent>
        <form onSubmit={submit} className="flex flex-col gap-4">
          <DialogHeader>
            <DialogTitle>New dataset mapping</DialogTitle>
            <DialogDescription>
              Select a declared domain and a Glue database to map into it. The
              dataset name is taken from the Glue database.
            </DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-4">
            <div className="flex flex-col gap-2">
              <Label htmlFor="new-mapping-source">Source</Label>
              <Select value={sourceType} disabled>
                <SelectTrigger id="new-mapping-source" className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectGroup>
                    {SOURCE_TYPES.map((s) => (
                      <SelectItem key={s.value} value={s.value}>
                        {s.label}
                      </SelectItem>
                    ))}
                  </SelectGroup>
                </SelectContent>
              </Select>
              <p className="text-muted-foreground text-xs">
                Only AWS Glue is supported today.
              </p>
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor="new-mapping-domain">Data domain</Label>
              <Select
                value={domain}
                onValueChange={setDomain}
                disabled={!declaredDomains.length}
              >
                <SelectTrigger id="new-mapping-domain" className="w-full">
                  <SelectValue
                    placeholder={
                      declaredDomains.length
                        ? "Select a domain..."
                        : "No domains declared — create one first"
                    }
                  />
                </SelectTrigger>
                <SelectContent>
                  <SelectGroup>
                    {declaredDomains.map((d) => (
                      <SelectItem key={d.data_domain} value={d.data_domain}>
                        {d.data_domain}
                        {d.description ? ` — ${d.description}` : ""}
                      </SelectItem>
                    ))}
                  </SelectGroup>
                </SelectContent>
              </Select>
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor="new-mapping-glue">Glue database</Label>
              <Select
                value={glueDatabase}
                onValueChange={setGlueDatabase}
                disabled={!databases.length}
              >
                <SelectTrigger id="new-mapping-glue" className="w-full">
                  <SelectValue
                    placeholder={
                      databases.length
                        ? "Select a Glue database..."
                        : "No Glue databases found"
                    }
                  />
                </SelectTrigger>
                <SelectContent>
                  <SelectGroup>
                    {databases.map((db) => (
                      <SelectItem key={db.name} value={db.name}>
                        {db.name}
                        {db.description ? ` — ${db.description}` : ""}
                      </SelectItem>
                    ))}
                  </SelectGroup>
                </SelectContent>
              </Select>
            </div>
            <p className="text-muted-foreground text-sm">
              Dataset:{" "}
              <span className="text-foreground font-mono">
                {domain || "<domain>"}/{dataset || "<glue database>"}
              </span>
            </p>
          </div>
          <DialogFooter>
            <DialogClose asChild>
              <Button type="button" variant="outline">
                Cancel
              </Button>
            </DialogClose>
            <Button type="submit" disabled={submitting || !declaredDomains.length}>
              {submitting ? <Spinner /> : null}
              Save mapping
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

// -- Mapping delete dialog --------------------------------------------------

function DeleteMappingDialog({ api, mapping, onDeleted }) {
  const [open, setOpen] = useState(false)
  const [deleting, setDeleting] = useState(false)

  const remove = async () => {
    setDeleting(true)
    try {
      await api.deleteDomainMapping(mapping.data_domain, mapping.dataset)
      toast.success(`Deleted ${mapping.data_domain}/${mapping.dataset}`)
      setOpen(false)
      onDeleted?.()
    } catch (err) {
      toast.error(`Could not delete mapping: ${err.message || err}`)
    } finally {
      setDeleting(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="ghost" size="icon-sm" aria-label="Delete mapping">
          <Trash2Icon />
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Delete mapping?</DialogTitle>
          <DialogDescription>
            This permanently deletes{" "}
            <span className="font-medium text-foreground">
              {mapping.data_domain}/{mapping.dataset}
            </span>{" "}
            ({mapping.glue_database}) and everything it owns: the registry
            entry, the harvested OKF bundle in S3, its search-index vectors, and
            its harvest history. The underlying Glue data is not touched. This
            cannot be undone — the bundle would have to be re-harvested.
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
