# Run and review harvests

A harvest authors and writes documentation for one dataset. Review and lint findings do not block publication.

## Run a full harvest

Use a full harvest for the first bundle. Also use it after broad source or context changes.

When a bundle exists, a full harvest rebuilds it from scratch. It discards authored documents, manual edits, applied annotations, verified computations, and cross-dataset references.

1. Select a dataset.
2. Open **Harvest**.
3. Open **Harvest settings**.
4. Review the Harvester, Sub-agent, and Reviewer settings.
5. Select **Start full harvest**.
6. Review the confirmation when a bundle exists.
7. Choose **Rebuild bundle**.
8. Monitor the run status.

Only one active lease writes a dataset at a time. A cancellation can mark the row terminal before the runtime has stopped.

Do not start another harvest until runtime activity has ended.

## Understand statuses

| Status | Meaning |
| --- | --- |
| Queued | The request was accepted and is waiting to start |
| Running | Agents are inspecting, authoring, or reviewing |
| Complete | The run reached completion and wrote its marker |
| Failed | The run ended with an error; partial live content can remain |
| Cancelled | The cancellation request was accepted; runtime termination is best-effort |

The detail field provides more information when a run fails.

Full harvest publication is not atomic. Readers can observe content while the run replaces the live tree.

After a failed or cancelled run, inspect version history. Restore the last completed version when the live tree is incomplete.

## Incremental harvests

Incremental harvests are available only for Glue mappings with an existing completed full harvest.

Glue catalog changes can start scoped work for an affected table. Some Iceberg data-only commits are skipped.

Incremental work preserves unrelated pages. It does not remove documentation for dropped tables.

Run a full harvest after table deletions or broad structural changes.

## Cross-dataset harvests

Open **More harvest options**, then choose **Cross-dataset discovery...**.

Both datasets must be distinct Glue mappings with completed bundles.

The run writes relationship pages only to the initiating dataset's bundle. A successful run can produce no pages.

Use this mode only when the datasets share a real business entity or workflow.

A later full harvest of the initiating dataset removes its cross-dataset pages. Run discovery again when you still need them.

## Review a completed run

Check the overview, table grain, joins, metrics, and caveats. Compare the new version when the change was narrow.

Use annotations or better context to correct weak pages. Then run the appropriate harvest again.

!!! tip
    Open CloudWatch GenAI Observability when Transaction Search is enabled. Otherwise, inspect AgentCore and CloudWatch logs.
