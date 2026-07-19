import { useCallback, useEffect, useRef, useState } from "react"
import { toast } from "sonner"
import {
  AlertTriangleIcon,
  CheckCircle2Icon,
  GaugeIcon,
  RefreshCwIcon,
  UploadIcon,
} from "lucide-react"

import { uploadToPresigned } from "@/lib/api"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"

const DEFAULTS = {
  enabled: false,
  max_iterations: 5,
}

// Normalize a server settings object into the local editable form state.
function toForm(s) {
  return {
    enabled: Boolean(s?.enabled),
    max_iterations: s?.max_iterations ?? DEFAULTS.max_iterations,
  }
}

// Configure the recursive-improvement benchmark for a dataset: upload the
// question,gold_sql CSV (to an OFF-MOUNT key so the gold is unreadable by the
// harvest agent) and set the iteration budget. The stop TARGET is fixed (the
// harvester keeps improving until the reviewed answers are ~90% good), because
// the point is to improve the wiki, not to tune a benchmark score. When enabled,
// every harvest of this dataset runs the benchmark→revise loop.
export default function BenchmarkView({ api, selection }) {
  const [form, setForm] = useState(toForm(null))
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState(null)
  // Parsed state of the uploaded question set (from the server, using the same
  // parser the harvest runtime uses). null until inspected.
  const [questions, setQuestions] = useState(null)
  const fileInputRef = useRef(null)

  const domain = selection?.data_domain
  const dataset = selection?.dataset
  const hasSelection = Boolean(domain && dataset)

  const inspect = useCallback(async () => {
    if (!api || !hasSelection) return
    try {
      setQuestions(await api.inspectBenchmarkQuestions(domain, dataset))
    } catch (e) {
      // A failure to inspect is shown inline, not as a hard page error.
      setQuestions({ uploaded: false, inspectError: e.message || String(e) })
    }
  }, [api, domain, dataset, hasSelection])

  const load = useCallback(async () => {
    if (!api || !hasSelection) return
    setLoading(true)
    setError(null)
    try {
      const res = await api.getBenchmarkSettings(domain, dataset)
      setForm(toForm(res?.recursive_improvement))
      await inspect()
    } catch (e) {
      setError(e.message || String(e))
    } finally {
      setLoading(false)
    }
  }, [api, domain, dataset, hasSelection, inspect])

  useEffect(() => {
    setForm(toForm(null))
    setQuestions(null)
    load()
  }, [load])

  const save = async () => {
    setSaving(true)
    try {
      const settings = {
        enabled: form.enabled,
        max_iterations: Number(form.max_iterations),
      }
      const res = await api.setBenchmarkSettings(domain, dataset, settings)
      setForm(toForm(res?.recursive_improvement))
      toast.success(
        settings.enabled
          ? "Benchmark enabled — it runs on the next harvest of this dataset."
          : "Benchmark disabled."
      )
    } catch (e) {
      toast.error(`Could not save: ${e.message || e}`)
    } finally {
      setSaving(false)
    }
  }

  const onPickFile = async (e) => {
    const file = e.target.files?.[0]
    if (fileInputRef.current) fileInputRef.current.value = ""
    if (!file || !hasSelection) return
    setUploading(true)
    try {
      const { url, fields, max_bytes } = await api.presignBenchmarkUpload(
        domain,
        dataset,
        file.type || "text/csv"
      )
      if (!url || !fields)
        throw new Error("presign response missing 'url'/'fields'")
      if (max_bytes && file.size > max_bytes) {
        throw new Error(
          `file is ${(file.size / 1048576).toFixed(1)} MB; max is ${(
            max_bytes / 1048576
          ).toFixed(0)} MB`
        )
      }
      await uploadToPresigned({ url, fields }, file)
      // Parse it server-side (same parser the runtime uses) so we can confirm the
      // format and report the exact question count — not just "uploaded".
      const parsed = await api.inspectBenchmarkQuestions(domain, dataset)
      setQuestions(parsed)
      if (parsed?.valid) {
        toast.success(
          `${parsed.count} question${parsed.count === 1 ? "" : "s"} extracted` +
            (parsed.capped
              ? ` (capped from ${parsed.total_in_csv} at ${parsed.max_questions})`
              : "")
        )
      } else {
        toast.error(`CSV format problem: ${parsed?.error || "could not parse"}`)
      }
    } catch (err) {
      toast.error(`Upload failed: ${err.message || err}`)
    } finally {
      setUploading(false)
    }
  }

  if (!hasSelection) {
    return (
      <Alert>
        <GaugeIcon />
        <AlertTitle>Select a dataset first</AlertTitle>
        <AlertDescription>
          Pick a dataset from the sidebar to set up its benchmark and
          auto-improvement.
        </AlertDescription>
      </Alert>
    )
  }

  return (
    <Card>
      <CardHeader className="border-b">
        <CardTitle className="flex items-center gap-2">
          <GaugeIcon className="size-4" />
          Benchmark &amp; auto-improve
          {form.enabled ? (
            <Badge variant="secondary">Enabled</Badge>
          ) : (
            <Badge variant="outline">Off</Badge>
          )}
        </CardTitle>
        <CardDescription>
          Test how well the wiki for{" "}
          <span className="font-medium text-foreground">
            {domain}/{dataset}
          </span>{" "}
          actually answers real questions, and let the harvester keep improving
          it until it does. Upload a set of questions with their correct SQL
          answers; when this is on, each harvest scores the wiki against them,
          fixes the gaps it finds, and re-tests — repeating until the reviewed
          answers are about 90% good or it runs out of iterations.
        </CardDescription>
        <div className="col-start-2 row-span-2 row-start-1 flex items-center gap-2 self-start justify-self-end">
          <Button variant="outline" onClick={load} disabled={loading}>
            {loading ? <Spinner /> : <RefreshCwIcon data-icon="inline-start" />}
            Refresh
          </Button>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-6">
        {error ? (
          <Alert variant="destructive">
            <AlertTitle>Failed to load benchmark settings</AlertTitle>
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        ) : loading ? (
          <div className="flex flex-col gap-2">
            <Skeleton className="h-8 w-full" />
            <Skeleton className="h-8 w-2/3" />
          </div>
        ) : (
          <>
            {/* Question set upload — lands OFF the okf/ mount so gold stays hidden. */}
            <div className="flex flex-col gap-2">
              <Label>Question set</Label>
              <p className="text-sm text-muted-foreground">
                A CSV with a <code>question</code> column and a{" "}
                <code>gold_sql</code> column (up to 100 rows are used). The gold
                SQL is stored off the harvest mount, so the authoring agent
                never sees the answers — it only learns what the wiki is
                missing.
              </p>
              <div>
                <input
                  ref={fileInputRef}
                  type="file"
                  className="hidden"
                  accept=".csv,text/csv"
                  onChange={onPickFile}
                />
                <Button
                  variant="outline"
                  onClick={() => fileInputRef.current?.click()}
                  disabled={uploading}
                >
                  {uploading ? (
                    <Spinner />
                  ) : (
                    <UploadIcon data-icon="inline-start" />
                  )}
                  {questions?.valid
                    ? "Replace questions CSV"
                    : "Upload questions CSV"}
                </Button>
              </div>
              <QuestionSetStatus questions={questions} />
            </div>

            {/* Loop settings. */}
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="flex flex-col gap-2">
                <Label htmlFor="bench-enabled">Status</Label>
                <Select
                  value={form.enabled ? "on" : "off"}
                  onValueChange={(v) =>
                    setForm((f) => ({ ...f, enabled: v === "on" }))
                  }
                >
                  <SelectTrigger id="bench-enabled" className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="off">Off</SelectItem>
                    <SelectItem value="on">Enabled</SelectItem>
                  </SelectContent>
                </Select>
              </div>

              <div className="flex flex-col gap-2">
                <Label htmlFor="bench-maxiter">Max iterations (2–5)</Label>
                <Input
                  id="bench-maxiter"
                  type="number"
                  min={2}
                  max={5}
                  value={form.max_iterations}
                  onChange={(e) =>
                    setForm((f) => ({ ...f, max_iterations: e.target.value }))
                  }
                />
                <p className="text-xs text-muted-foreground">
                  How many benchmark→improve rounds the harvester may run before it
                  has to stop.
                </p>
              </div>
            </div>

            <div className="flex items-center gap-2">
              <Button onClick={save} disabled={saving}>
                {saving ? <Spinner data-icon="inline-start" /> : null}
                Save settings
              </Button>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  )
}

// Inline feedback on the uploaded question set: nothing yet, a valid count (with
// a cap note), or a format error. Mirrors the server's inspect response shape.
function QuestionSetStatus({ questions }) {
  if (!questions) return null

  if (questions.inspectError) {
    return (
      <p className="text-sm text-muted-foreground">
        Could not read the question set: {questions.inspectError}
      </p>
    )
  }

  if (!questions.uploaded) {
    return (
      <p className="text-sm text-muted-foreground">
        No question set uploaded yet.
      </p>
    )
  }

  if (!questions.valid) {
    return (
      <Alert variant="destructive">
        <AlertTriangleIcon />
        <AlertTitle>Invalid question set</AlertTitle>
        <AlertDescription>
          {questions.error || "The CSV could not be parsed."} Expected a{" "}
          <code>question</code> column and a <code>gold_sql</code> column.
        </AlertDescription>
      </Alert>
    )
  }

  return (
    <div className="flex items-start gap-2 text-sm">
      <CheckCircle2Icon className="mt-0.5 size-4 shrink-0 text-emerald-600 dark:text-emerald-500" />
      <span className="text-muted-foreground">
        <span className="font-medium text-foreground">
          {questions.count} question{questions.count === 1 ? "" : "s"}
        </span>{" "}
        will be benchmarked.
        {questions.capped
          ? ` Capped from ${questions.total_in_csv} rows at the ${questions.max_questions}-question limit (first ${questions.max_questions} used).`
          : ""}
      </span>
    </div>
  )
}
