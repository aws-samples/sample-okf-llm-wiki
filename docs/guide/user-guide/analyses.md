# Run saved analyses

An analysis is a saved procedure that the chat agent follows step by step. Use it to repeat the same investigation with new inputs.

## How an analysis works

Each analysis belongs to one dataset. It has a title, a description, questions, numbered steps, and a finish criterion.

The questions collect the inputs that a run needs, such as a period or a segment. Analyses have no default answers.

Steps cite the wiki pages and computations they rely on. A run follows the steps as written and reports any deviation.

## Create an analysis

Ask the agent in **Chat** to save an analysis for a dataset. Describe the goal, the inputs, and the expected result.

The agent drafts the document and validates it before saving. You become the owner of the analysis.

Ask the agent to change a step later. It edits the saved document and increments its version.

## Run an analysis

1. Open **Chat**.
2. Choose **+** > **Run An Analysis**.
3. Search for the analysis. Filter by dataset when needed.
4. Answer its questions.
5. Choose **Run**.

The run starts with your answers, so the agent does not ask them again. A scoped conversation lists only that dataset's analyses.

A finished run usually produces a report. The agent publishes that report to the analysis, so it outlives the conversation.

## Manage analyses

Open **Analysis** in the sidebar. It has two tabs.

| Tab | Contents |
| --- | --- |
| **Analyses** | Every saved analysis, with its dataset, version, and published report count |
| **Reports** | Every published report, filterable by dataset and analysis |

Open an analysis to read it. Owners can choose **Edit** to change the full document, or **Delete** to remove it.

Deleting an analysis removes its report links. The report files remain available.

Anyone with console access can read and run any analysis. Only the owner can edit or delete it.

!!! note
    Deleting a dataset also deletes its analyses.

## Next step

[Connect an external agent](connect-an-agent.md).
