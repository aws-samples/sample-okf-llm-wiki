import { AlertTriangleIcon, CircleXIcon, UploadIcon } from "lucide-react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Spinner } from "@/components/ui/spinner"

// One validation row — blockers and lint findings share the {code, path,
// message} shape (findings add severity).
function FindingRow({ icon, severity, path, message }) {
  return (
    <li className="flex items-start gap-2 py-1.5 text-sm">
      {icon}
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          {severity ? (
            <Badge
              variant="outline"
              className={
                severity === "error"
                  ? "border-destructive/30 text-destructive"
                  : "border-amber-600/30 text-amber-600 dark:text-amber-500"
              }
            >
              {severity}
            </Badge>
          ) : null}
          {path ? (
            <span className="truncate font-mono text-xs text-muted-foreground">
              {path}
            </span>
          ) : null}
        </div>
        <p className="mt-0.5 wrap-anywhere text-muted-foreground">{message}</p>
      </div>
    </li>
  )
}

// The validate report, rendered for the human decision the import contract
// promises: BLOCKERS (structural/safety — the server refuses these outright)
// versus FINDINGS (the same offline lint a harvest gate runs, plus EXPLAIN
// failures against this deployment's catalog) which the user may acknowledge
// and import anyway.
export function ImportBundleDialog({
  open,
  onOpenChange,
  report,
  fileName,
  applying,
  onConfirm,
}) {
  const blockers = report?.blockers || []
  const findings = report?.findings || []
  const sql = report?.sql || {}
  const manifest = report?.manifest
  const exportedAt = manifest?.exported_at
    ? new Date(manifest.exported_at).toLocaleString()
    : null
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Import bundle?</DialogTitle>
          <DialogDescription>
            {fileName ? (
              <span className="font-medium text-foreground">{fileName}</span>
            ) : (
              "The uploaded archive"
            )}{" "}
            — {report?.files ?? 0} files, {report?.docs ?? 0} docs
            {report?.tables?.length ? `, ${report.tables.length} tables` : ""}
            {manifest
              ? ` · exported from ${manifest.data_domain}/${manifest.dataset}${
                  exportedAt ? ` on ${exportedAt}` : ""
                }`
              : ""}
            . Importing replaces the live bundle; the previous version stays
            restorable from History. Computation verification does not travel —
            imported computations arrive unverified.
          </DialogDescription>
        </DialogHeader>
        {blockers.length > 0 && (
          <div>
            <p className="text-sm font-medium text-destructive">
              {blockers.length} blocker{blockers.length === 1 ? "" : "s"} — the
              archive cannot be imported
            </p>
            <ScrollArea className="mt-1 max-h-48">
              <ul className="divide-y">
                {blockers.map((b, i) => (
                  <FindingRow
                    key={i}
                    icon={
                      <CircleXIcon className="mt-0.5 size-4 shrink-0 text-destructive" />
                    }
                    path={b.path}
                    message={b.message}
                  />
                ))}
              </ul>
            </ScrollArea>
          </div>
        )}
        {findings.length > 0 && (
          <div>
            <p className="text-sm font-medium">
              {findings.length} lint finding{findings.length === 1 ? "" : "s"} —
              review before importing
            </p>
            <ScrollArea className="mt-1 max-h-48">
              <ul className="divide-y">
                {findings.map((f, i) => (
                  <FindingRow
                    key={i}
                    icon={
                      <AlertTriangleIcon className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
                    }
                    severity={f.severity}
                    path={f.path}
                    message={f.message}
                  />
                ))}
              </ul>
            </ScrollArea>
          </div>
        )}
        {blockers.length === 0 && (
          <p className="text-xs text-muted-foreground">
            {sql.checked || sql.skipped
              ? `SQL check: ${sql.checked || 0} validated against this ` +
                `deployment's catalog, ${sql.failed || 0} failed` +
                (sql.skipped ? `, ${sql.skipped} skipped` : "") +
                (sql.note ? ` (${sql.note})` : "")
              : sql.note || "No runnable SQL to check."}
          </p>
        )}
        <DialogFooter>
          <Button
            variant="outline"
            disabled={applying}
            onClick={() => onOpenChange(false)}
          >
            Cancel
          </Button>
          <Button
            disabled={blockers.length > 0 || applying}
            onClick={onConfirm}
          >
            {applying ? <Spinner /> : <UploadIcon className="size-3.5" />}
            {findings.length > 0 ? "Import anyway" : "Import"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
