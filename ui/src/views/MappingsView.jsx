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
import { Input } from "@/components/ui/input"
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

// Whether the deployment carries the Redshift IAM grants (var.enable_redshift,
// baked at build time). Without them the pickers can only ever come back empty,
// so don't offer the option at all. UX gating — the backend still validates.
const REDSHIFT_ENABLED =
  String(import.meta.env.VITE_REDSHIFT_ENABLED || "") === "true"

// Source types a mapping can draw from. This list is the single place the UI
// knows about sources, so adding a type later (BigQuery, …) is a one-line change
// here + backend support. Mirrors okf_core.sources.SUPPORTED_SOURCE_TYPES,
// filtered to what this deployment enables.
const SOURCE_TYPES = [
  { value: "glue", label: "AWS Glue" },
  ...(REDSHIFT_ENABLED
    ? [{ value: "redshift", label: "Amazon Redshift" }]
    : []),
]
const DEFAULT_SOURCE_TYPE = "glue"

// Human label for a source type (falls back to the raw value / a dash).
// Covers EVERY known type (not just the offered SOURCE_TYPES): an existing
// Redshift mapping row must still render its label on a deployment where the
// Redshift option isn't offered for NEW mappings.
const SOURCE_LABELS = { glue: "AWS Glue", redshift: "Amazon Redshift" }

function sourceLabel(type) {
  if (!type) return "—"
  return SOURCE_LABELS[type] || type
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
    // h-full so the card fills the content region; the table body then scrolls
    // internally (sticky header) instead of the whole card scrolling as a block.
    <div className="flex h-full flex-col gap-4">
      <Card className="min-h-0 flex-1">
        <CardHeader className="border-b">
          <CardTitle className="flex items-center gap-2">
            <DatabaseIcon className="size-4" />
            Dataset mappings
          </CardTitle>
          <CardDescription>
            Map a data source into a declared domain to start harvesting it.
          </CardDescription>
          <div className="col-start-2 row-span-2 row-start-1 flex items-center gap-2 self-start justify-self-end">
            <Button variant="outline" onClick={refresh} disabled={loading}>
              {loading ? (
                <Spinner />
              ) : (
                <RefreshCwIcon data-icon="inline-start" />
              )}
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
        <CardContent className="flex min-h-0 flex-1 flex-col">
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
                  ? "Declare a domain first (Domains), then create a mapping here to start harvesting a data source."
                  : 'Create one with the "New mapping" button to start harvesting a data source.'}
              </AlertDescription>
            </Alert>
          ) : (
            <Table containerClassName="min-h-0 flex-1 overflow-y-auto">
              {/* Stick the th cells (not the thead — sticky on thead is flaky
                  cross-browser) so the header pins while the body scrolls. bg-card
                  keeps rows from showing through; a bottom ring stands in for the
                  row border, which scrolls away with a sticky element. */}
              <TableHeader className="[&_th]:sticky [&_th]:top-0 [&_th]:z-10 [&_th]:bg-card [&_th]:shadow-[inset_0_-1px_0_var(--border)] [&_tr]:border-0 [&_tr:hover]:bg-transparent">
                <TableRow>
                  <TableHead>Data domain</TableHead>
                  <TableHead>Dataset</TableHead>
                  <TableHead>Source</TableHead>
                  <TableHead>Database</TableHead>
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
                      {d.source?.glue_database ||
                        d.source?.redshift_database ||
                        d.glue_database ||
                        "—"}
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
  const [sourceType, setSourceType] = useState(DEFAULT_SOURCE_TYPE)
  const [glueDatabase, setGlueDatabase] = useState("")
  // Redshift: pick a cluster/workgroup (loaded from the account), give the secret
  // that authenticates to it, then pick a database within it (loaded on demand).
  // The dataset name is the operator's own id — it differs from the database (a
  // Redshift database holds many schemas), unlike Glue where dataset == database.
  const [rsTargets, setRsTargets] = useState([]) // [{kind,id,database?}]
  const [rsTargetsLoaded, setRsTargetsLoaded] = useState(false)
  const [rsTargetsLoading, setRsTargetsLoading] = useState(false)
  const [rsTarget, setRsTarget] = useState("") // "kind:id"
  const [rsSecretArn, setRsSecretArn] = useState("")
  const [rsDatabases, setRsDatabases] = useState([])
  const [rsDatabasesLoading, setRsDatabasesLoading] = useState(false)
  const [redshiftDatabase, setRedshiftDatabase] = useState("")
  const [redshiftDataset, setRedshiftDataset] = useState("")
  const [submitting, setSubmitting] = useState(false)

  const isGlue = sourceType === "glue"
  const dataset = isGlue ? glueDatabase : redshiftDataset
  // "kind:id" -> {kind, id}. kind is "cluster" | "workgroup".
  const [rsTargetKind, rsTargetId] = rsTarget
    ? rsTarget.split(/:(.+)/)
    : ["", ""]
  // The selected target's default DB (a cluster's DBName) — the bootstrap
  // database ListDatabases connects to. A cluster created with a custom initial
  // DB may have no "dev", so this hint matters; workgroups always have "dev".
  const rsBootstrapDb =
    rsTargets.find((t) => t.kind === rsTargetKind && t.id === rsTargetId)
      ?.database || ""

  // Load Redshift targets ONCE, the first time the user switches to Redshift.
  // Guarded by a loaded flag — NOT by rsTargets.length: an account with zero
  // targets (or an error) would otherwise settle back into the trigger state and
  // refetch forever. Reopening the dialog is the retry path.
  useEffect(() => {
    if (sourceType !== "redshift" || rsTargetsLoaded || rsTargetsLoading) return
    setRsTargetsLoading(true)
    api
      .listRedshiftClusters()
      .then((t) => setRsTargets(t || []))
      .catch((err) =>
        toast.error(`Could not list Redshift targets: ${err.message || err}`)
      )
      .finally(() => {
        setRsTargetsLoaded(true)
        setRsTargetsLoading(false)
      })
  }, [sourceType, rsTargetsLoaded, rsTargetsLoading, api])

  // Load databases for the selected target once we have a target + a secret.
  const loadRedshiftDatabases = async () => {
    if (!rsTargetId || !rsSecretArn) {
      toast.error("Pick a cluster/workgroup and enter its secret ARN first.")
      return
    }
    setRsDatabasesLoading(true)
    setRedshiftDatabase("")
    try {
      const dbs = await api.listRedshiftDatabases({
        kind: rsTargetKind,
        id: rsTargetId,
        secretArn: rsSecretArn,
        database: rsBootstrapDb,
      })
      setRsDatabases(dbs || [])
      if (!dbs?.length) toast.info("No databases returned for that target.")
    } catch (err) {
      toast.error(`Could not list databases: ${err.message || err}`)
    } finally {
      setRsDatabasesLoading(false)
    }
  }

  const reset = () => {
    setDomain("")
    setSourceType(DEFAULT_SOURCE_TYPE)
    setGlueDatabase("")
    setRsTargets([])
    setRsTargetsLoaded(false) // reopening the dialog re-fetches targets
    setRsTarget("")
    setRsSecretArn("")
    setRsDatabases([])
    setRedshiftDatabase("")
    setRedshiftDataset("")
  }

  const submit = async (e) => {
    e.preventDefault()
    if (!domain) {
      toast.error("Select a domain.")
      return
    }
    if (isGlue && !glueDatabase) {
      toast.error("Select a Glue database.")
      return
    }
    let source
    if (isGlue) {
      source = { type: "glue", glue_database: glueDatabase }
    } else {
      if (
        !rsTargetId ||
        !rsSecretArn ||
        !redshiftDatabase ||
        !redshiftDataset
      ) {
        toast.error(
          "Pick a cluster/workgroup, its secret, a database, and a dataset name."
        )
        return
      }
      source = {
        type: "redshift",
        redshift_database: redshiftDatabase,
        secret_arn: rsSecretArn,
        ...(rsTargetKind === "cluster"
          ? { cluster_identifier: rsTargetId }
          : { workgroup_name: rsTargetId }),
      }
    }
    setSubmitting(true)
    try {
      await api.setDomainMapping(domain, dataset, source)
      toast.success(`Mapped ${domain}/${dataset}`)
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
    <Dialog
      open={open}
      onOpenChange={(v) => {
        setOpen(v)
        if (!v) reset() // closing discards the draft; reopening starts clean
      }}
    >
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
              Select a declared domain and a data source to map into it.
            </DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-4">
            <div className="flex flex-col gap-2">
              <Label htmlFor="new-mapping-source">Source</Label>
              <Select value={sourceType} onValueChange={setSourceType}>
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
              {!isGlue ? (
                <p className="text-xs text-muted-foreground">
                  Pick a cluster or workgroup, give the Secrets Manager secret
                  that connects to it, then load and pick a database.
                </p>
              ) : null}
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
            {isGlue ? (
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
            ) : (
              <>
                <div className="flex flex-col gap-2">
                  <Label htmlFor="new-mapping-rs-target">
                    Cluster / workgroup
                  </Label>
                  <Select
                    value={rsTarget}
                    onValueChange={(v) => {
                      setRsTarget(v)
                      setRsDatabases([])
                      setRedshiftDatabase("")
                    }}
                    disabled={rsTargetsLoading || !rsTargets.length}
                  >
                    <SelectTrigger
                      id="new-mapping-rs-target"
                      className="w-full"
                    >
                      <SelectValue
                        placeholder={
                          rsTargetsLoading
                            ? "Loading Redshift targets..."
                            : rsTargets.length
                              ? "Select a cluster or workgroup..."
                              : "No Redshift clusters or workgroups found"
                        }
                      />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectGroup>
                        {rsTargets.map((t) => (
                          <SelectItem
                            key={`${t.kind}:${t.id}`}
                            value={`${t.kind}:${t.id}`}
                          >
                            {t.id} ({t.kind})
                          </SelectItem>
                        ))}
                      </SelectGroup>
                    </SelectContent>
                  </Select>
                </div>
                <div className="flex flex-col gap-2">
                  <Label htmlFor="new-mapping-rs-secret">
                    Connection secret ARN
                  </Label>
                  <Input
                    id="new-mapping-rs-secret"
                    value={rsSecretArn}
                    onChange={(e) => setRsSecretArn(e.target.value)}
                    placeholder="arn:aws:secretsmanager:...:secret:..."
                  />
                  <p className="text-xs text-muted-foreground">
                    Secrets Manager secret with the DB credentials used to read
                    this cluster/workgroup. Use a <strong>read-only</strong>{" "}
                    database user — SQL runs with this user's privileges — and
                    name the secret with the deployment's prefix (default{" "}
                    <span className="font-mono">okf-</span>) so the service is
                    allowed to read it.
                  </p>
                </div>
                <div className="flex flex-col gap-2">
                  <Label htmlFor="new-mapping-redshift-db">Database</Label>
                  <div className="flex gap-2">
                    <Select
                      value={redshiftDatabase}
                      onValueChange={setRedshiftDatabase}
                      disabled={!rsDatabases.length}
                    >
                      <SelectTrigger
                        id="new-mapping-redshift-db"
                        className="w-full"
                      >
                        <SelectValue
                          placeholder={
                            rsDatabases.length
                              ? "Select a database..."
                              : "Load databases →"
                          }
                        />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectGroup>
                          {rsDatabases.map((db) => (
                            <SelectItem key={db} value={db}>
                              {db}
                            </SelectItem>
                          ))}
                        </SelectGroup>
                      </SelectContent>
                    </Select>
                    <Button
                      type="button"
                      variant="outline"
                      onClick={loadRedshiftDatabases}
                      disabled={
                        rsDatabasesLoading || !rsTargetId || !rsSecretArn
                      }
                    >
                      {rsDatabasesLoading ? <Spinner /> : "Load"}
                    </Button>
                  </div>
                </div>
                <div className="flex flex-col gap-2">
                  <Label htmlFor="new-mapping-redshift-dataset">
                    Dataset name
                  </Label>
                  <Input
                    id="new-mapping-redshift-dataset"
                    value={redshiftDataset}
                    onChange={(e) => setRedshiftDataset(e.target.value)}
                    placeholder="e.g. orders_analytics"
                  />
                </div>
              </>
            )}
            <p className="text-sm text-muted-foreground">
              Dataset:{" "}
              <span className="font-mono text-foreground">
                {domain || "<domain>"}/{dataset || "<dataset>"}
              </span>
            </p>
          </div>
          <DialogFooter>
            <DialogClose asChild>
              <Button type="button" variant="outline">
                Cancel
              </Button>
            </DialogClose>
            <Button
              type="submit"
              disabled={submitting || !declaredDomains.length}
            >
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
            and everything it owns: the registry entry, the harvested OKF bundle
            in S3, its search-index vectors, and its harvest history. The
            underlying source data is not touched. This cannot be undone — the
            bundle would have to be re-harvested.
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
