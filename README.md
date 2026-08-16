# Copilot AHP harness

This is a minimal external Python client for VS Code's Agent Host Protocol (AHP).
It starts an isolated `code agent host`, authenticates the Copilot adapter with your GitHub credentials, creates a workspace-scoped session, and streams the conversation in a terminal.
It does not use the VS Code Copilot Chat UI.

## Requirements

- VS Code with `code agent host` support.
- Python 3.11 or newer.
- A GitHub account with Copilot access.
- Either `COPILOT_GITHUB_TOKEN`, `GH_TOKEN`, or `GITHUB_TOKEN`, or an authenticated GitHub CLI installation.

The harness never prints the GitHub token or places it in process arguments.
The Agent Host transport token is passed through a mode-0600 temporary file.
One-shot requests dispose their AHP session and stop the dedicated Agent Host process tree before Python exits.

## Install

```sh
cd copilot-ahp-harness
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

## Use

Start an interactive chat:

```sh
copilot-ahp --workspace /path/to/lab
```

Run one request:

```sh
copilot-ahp --workspace /path/to/lab \
  --query "Inspect this project and summarize its attack surface"
```

Read a multiline one-shot request from standard input:

```sh
copilot-ahp --workspace /path/to/lab --query - <<'EOF'
Inspect this project.
Focus on authentication and trust boundaries.
EOF
```

Interactive mode supports these commands:

```text
/help             Show command help
/model            List available models
/model MODEL_ID   Select a model
/status           Show the workspace, approval policy, model, and session
/paste            Enter a multiline prompt, ending with a line containing only .
/new              Dispose the current session and create a new one
/exit             Exit
```

Show the live model catalog exposed by the Agent Host:

```sh
copilot-ahp --list-models
```

Select a model by its advertised ID:

```sh
copilot-ahp --model MODEL_ID --query "Review this project"
```

The harness prints the effective model before it creates a session.
The current VS Code 1.133 Copilot adapter may advertise only the `auto` model, depending on the bundled runtime, account, and service-side catalog.

## Terminal output

Interactive TTYs show a spinner while Copilot is thinking or running a tool.
The spinner is cleared before model text, approval questions, and Ask User prompts.
It is disabled automatically when standard error is not a TTY.

Press Ctrl-C once during an active turn to ask the Agent Host to cancel it while preserving the interactive session.
Press Ctrl-C again to exit immediately.

The model response is written to standard output.
Model selection, tool activity, prompts, and the completion summary are written to standard error.
This lets scripts capture the response without terminal status messages.

Control color and spinner behavior explicitly when needed:

```sh
copilot-ahp --color never --spinner never --query "Summarize this project"
```

Color is disabled automatically for non-TTY output, `TERM=dumb`, or when `NO_COLOR` is set.

Use JSON Lines output for automation:

```sh
copilot-ahp --output jsonl --query "Inspect authentication"
```

Each standard-output line is an independent JSON object.
The stream includes turn lifecycle records and the raw AHP actions observed during the turn.
Human approval and Ask User prompts remain on standard error so they never corrupt the JSON stream.

At the end of a text-mode turn, the harness reports elapsed time, tool count, cancellation state, and token usage when the provider supplies it.

Tool calls require an interactive confirmation by default.
Use `--approval deny` to prevent all confirmable tools or `--approval all` only inside a disposable lab environment.

When Copilot invokes Ask User, the harness displays every question and option in the terminal.
Enter an option number, custom text when offered, or comma-separated numbers for multi-select questions.
Use `/skip` for optional questions, `/decline` to decline the entire request, or `/cancel` to cancel it.

To connect to an already-running standalone host without putting its token in shell history:

```sh
export AHP_CONNECTION_TOKEN='...'
copilot-ahp --connect ws://127.0.0.1:43129
```

## Architecture and caveats

```text
Python harness
    -> AHP 0.8 JSON-RPC over authenticated WebSocket
    -> VS Code Agent Host
    -> Copilot adapter
    -> GitHub Copilot service
    -> Agent Host tools and workspace
```

This removes the Copilot UI and does not require you to install or invoke the public `copilot` CLI yourself.
The current VS Code Agent Host implementation still backs its `copilotcli` adapter with GitHub's bundled Copilot SDK/runtime.
This is therefore a different harness around the same underlying runtime, not a direct undocumented Copilot HTTP client.

VS Code 1.133.0 currently identifies the provider as `copilotcli` and expects provider-scoped session URIs such as `copilotcli:/<uuid>`.
That differs from the generic `ahp-session:/<uuid>` examples in the AHP documentation and may change while AHP is evolving.
