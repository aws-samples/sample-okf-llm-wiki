# Manage domains and datasets

Use domains to organize datasets by business area. Use mappings to connect each dataset to its physical source.

## Create a domain

1. Open **Domains**.
2. Choose **New domain**.
3. Enter a stable name.
4. Add a short description and useful context.
5. Choose **Create domain**.

Use names that people already use. Avoid temporary project names or team-specific abbreviations.

## Register a Glue dataset

1. Open **Datasets**.
2. Choose **New mapping**.
3. Select **AWS Glue**.
4. Select **Data domain**.
5. Choose the **Glue database**.
6. Choose **Save mapping**.

The dataset identifier follows the selected source database.

## Register a Redshift dataset

Enable Redshift support during the compute deployment. Then create a mapping in **Datasets**.

1. Choose **New mapping**.
2. Select **Amazon Redshift**.
3. Select **Data domain**.
4. Choose the cluster or workgroup.
5. Enter the **Connection secret ARN**.
6. Choose **Load**.
7. Select the database.
8. Enter a dataset name.
9. Choose **Save mapping**.

The secret must contain a read-only database user. The dataset name is separate from the Redshift database name.

## Choose the active dataset

Use the dataset picker before you open a dataset-scoped page. Browse, Graph, Context Docs, Harvest, and Benchmark use that selection.

## Delete a mapping

Deleting a mapping removes its Data Wiki state and published bundle. It does not delete the underlying source database.

!!! warning
    Deletion cannot be undone through the console. Export important content before you continue.

Recovery requires recreating the mapping and running another harvest. Retained S3 object versions are not a supported mapping recovery workflow.

## Next step

[Add context documents](context-documents.md).
