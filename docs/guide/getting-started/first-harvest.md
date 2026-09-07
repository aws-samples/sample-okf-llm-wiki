# Run your first harvest

Create a domain, register a dataset, and publish its first bundle.

## Create a data domain

1. Open **Domains**.
2. Choose **New domain**.
3. Enter a clear business name.
4. Add a short description of the domain.
5. Choose **Create domain**.

A domain groups datasets that share business context. Examples include sales, finance, fulfillment, and customer support.

## Register a dataset

1. Open **Datasets**.
2. Choose **New mapping**.
3. Select the source type.
4. Select the data domain.

For AWS Glue:

1. Choose the Glue database.
2. Choose **Save mapping**.

For Amazon Redshift:

1. Choose a cluster or workgroup.
2. Enter the connection secret ARN.
3. Choose **Load**.
4. Select the database.
5. Enter a dataset name.
6. Choose **Save mapping**.

The dataset becomes available in the dataset picker.

## Add context

Open **Context Docs** for the dataset. Upload data dictionaries, definitions, runbooks, or schema notes.

Add context before the first harvest when possible. Good context improves names, joins, metrics, and caveats.

## Start the harvest

1. Open **Harvest**.
2. Open **Harvest settings**.
3. Review the Harvester, Sub-agent, and Reviewer settings.
4. Select **Start full harvest**.
5. Wait for the run to finish.

The service uses a dataset lease to prevent normal concurrent harvest starts.

## Review the result

Open **Browse** after the status becomes complete.

Check these items:

- The dataset overview matches its business purpose.
- Each table has the correct grain.
- Important joins are present and accurate.
- Metrics use the right filters and units.
- Known issues describe nulls, sentinels, and source limits.

Use **Graph** to find isolated pages or missing relationships.

!!! note
    Do not hide a source problem with documentation. Fix the source when the catalog or data is wrong.

## Next step

[Connect an agent](../user-guide/connect-an-agent.md).
