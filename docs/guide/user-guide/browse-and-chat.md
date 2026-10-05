# Browse and chat

Use Browse for direct reading. Use Chat when you want an answer across several concepts.

## Browse a bundle

1. Select a dataset.
2. Open **Browse**.
3. Expand the concept tree.
4. Open a page.
5. Follow links to related concepts.

The URL tracks the open concept. You can share that URL with another authorized console user.

Use version history to compare or restore published bundle versions.

## View the graph

Open **Graph** to see concepts and their links.

Use the graph to find:

- Tables with no useful links
- Missing join documentation
- Dense areas that may need an overview
- Cross-dataset references

## Ask the wiki

Open **Chat** and enter a question. Type `@` or choose **+** > **Scope To A Dataset** when the question belongs to one dataset.

The agent reads wiki pages before it answers. Wiki reads and optional SQL access are read-only.

When requested, Chat can save report artifacts. It can also file annotations after user confirmation.

## Composer controls

The row under the message box holds the run settings.

| Control | Purpose |
| --- | --- |
| **Guardrails** | Select **Disabled**, **Computational**, **Behavioural**, or **Strict** checks on the agent's SQL |
| **SQL** | Allow read-only SQL against the source data |
| Model and **Effort** | Choose the model and its reasoning effort |
| Context ring | Show how much of the model's context window the conversation uses |

Guardrails only apply while **SQL** is on. The control shows **Inactive Without SQL** otherwise.

Hover the model name in the **Effort** card to switch models. A started conversation can switch only within its provider family.

## Long conversations

Open the context ring to see usage and the compaction threshold. Choose **Compact Now** to summarize older turns early.

Chat also compacts automatically near the context limit. Compaction never deletes messages, and a divider marks where it happened.

Use the turn rail on the left of the transcript to jump between questions. Hover it to list every question.

## Clarifying questions

The agent can pause and ask you questions before it continues. Answer them in the form that replaces the message box.

You can reload or leave the page while an answer runs. Reopen the conversation to follow the rest of the answer.

## Review an answer

Open the cited concepts. Confirm that the answer matches the source pages and their caveats.

Ask a narrower follow-up when the question has unclear time, region, metric, or grain.

!!! note
    Chat can produce a confident but incorrect answer. Verify high-impact results against the cited pages and source data.

## Next step

[Run saved analyses](analyses.md).
