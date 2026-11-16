# AI Browser

**English** | [Русский](README.ru.md)

![AI Browser logo](extension/icons/logo-128.png)

**Let your AI agent work with selected pages in your everyday Chrome browser.**

AI Browser connects Chrome to Codex or Claude so your agent can read pages, follow links, and fill in forms at your request. You keep using your existing browser, profiles, and signed-in accounts.

The project includes a Chrome extension and a small local program. They connect to the agent through MCP, a standard for adding tools to AI clients. You do not need a separate browser, Docker, or an exposed remote debugging port.

The setup below uses **macOS, Google Chrome, and an AI client with local MCP support**, such as Codex, Claude Code, or Claude Desktop.

## When it helps

- Ask your agent to read an open page and explain what it says.
- Fill in a form, choose a date, check options, and attach a specific file.
- Test a web interface while developing it.
- Work through a task on a website while keeping the results visible in Chrome.
- Run separate tasks in separate tab groups.

For example:

> Open example.com and tell me what is on the page.
>
> Read the selected tab and summarize the main terms.
>
> Fill in this form with the details below. Show me the result before submitting it.

## How it compares with Claude and OpenAI browser tools

AI Browser's main advantage is **one local MCP server for both Codex and Claude**. You can switch AI clients without replacing the browser extension or local bridge. The extension and server source code is available in this repository, so you can inspect the implementation and adapt the setup to your workflow.

| Option | A good fit when | What to consider |
| --- | --- | --- |
| **AI Browser** | You want a separate MCP tool for Codex or Claude, including terminal workflows | Manual setup; the browser integration currently targets macOS and Google Chrome |
| [**Claude in Chrome**](https://code.claude.com/docs/en/chrome) | You want an integrated Claude and Claude Code experience with website permissions and Anthropic support | Part of the Claude ecosystem, subject to its access requirements |
| OpenAI's [**ChatGPT browser extension**](https://learn.chatgpt.com/docs/chrome-extension) | You want an integrated ChatGPT experience with side chat and website permission controls | Availability depends on your ChatGPT plan and workspace settings |

The official extensions also use your existing browser sign-ins. Claude already offers its own Chrome MCP integration and session tab groups, so these features are not unique to AI Browser. OpenAI's built-in [Browser](https://learn.chatgpt.com/docs/browser) is a separate option with its own profile rather than your everyday Chrome; it is unavailable in Codex CLI and the IDE extension.

Choose AI Browser when you want a standalone tool for both clients. The official extensions are a better fit when you prefer a ready-made interface and website permission controls without configuring your own bridge. The project has no comparative benchmarks for speed, token usage, or security.

## What the agent can do

| Task | Capabilities |
| --- | --- |
| Read a website | Page text, links, fields, and buttons; screenshots for visual checks |
| Navigate | Open pages, follow links, and select tabs within its group |
| Complete forms | Enter text, choose options, and work with checkboxes, dates, and editable content |
| Upload files | Select existing local files for a file upload field |
| Use complex controls | Use keyboard input and screenshot-based clicks when ordinary page elements are not enough |
| Continue a task | Recover from temporary connection loss; reload the Python component without closing tabs |

Embedded pages from a different origin are not accessible. Same-origin iframes and Shadow DOM elements are supported. Some websites may still need manual interaction.

## Requirements

- macOS with Google Chrome installed.
- Python **3.11 or later** and Git.
- An AI client with support for local MCP servers: Codex, Claude Code, or Claude Desktop. The CLI examples use the `codex` or `claude` command.
- Your usual access to an AI model through your chosen client. AI Browser itself needs no OpenAI or Anthropic API key; your AI client's terms and costs still apply.

## Installation

Run these commands in Terminal. Keep the project folder in its chosen location after setup: Chrome and your AI client will use paths inside it.

### 1. Download and install the project

```sh
git clone https://github.com/d0grr/ai-browser.git
cd ai-browser
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-mcp.lock
.venv/bin/python -m pip install --no-deps -e '.[mcp]'
```

### 2. Add the Chrome extension

1. Open `chrome://extensions`.
2. Turn on **Developer mode**.
3. Click **Load unpacked** and select the project's `extension` folder.
4. Copy the **ID** shown on the AI Browser extension card.

### 3. Connect the extension to the local program

From the same project folder, run the following command, replacing `EXTENSION_ID` with the ID you copied:

```sh
.venv/bin/ai-browser install EXTENSION_ID
```

This creates Native Messaging settings in your user account's Chrome folder. Native Messaging lets the extension communicate with the local program. If you loaded the extension before this step, reload it using the button on its card at `chrome://extensions`.

### 4. Connect AI Browser to your AI client

Choose the command for your client.

**Codex:**

```sh
codex mcp add ai-browser -- "$PWD/.venv/bin/python" -m ai_browser.mcp_server
codex mcp list
```

**Claude Code:**

```sh
claude mcp add --transport stdio --scope user ai-browser -- "$PWD/.venv/bin/python" -m ai_browser.mcp_server
claude mcp list
```

Both commands register the server for your user account, so it is also available when you work in other folders. Claude Code also lists tools through `/mcp`. See the [Claude Code MCP guide](https://code.claude.com/docs/en/mcp#option-3-add-a-local-stdio-server) for details.

**Claude Desktop:**

In `~/Library/Application Support/Claude/claude_desktop_config.json`, add `ai-browser` to the existing `mcpServers` object. Keep any other servers and settings. Replace `/absolute/path/to/ai-browser` with the full project path, which you can find by running `pwd` in the project folder:

```json
{
  "mcpServers": {
    "ai-browser": {
      "command": "/absolute/path/to/ai-browser/.venv/bin/python",
      "args": ["-m", "ai_browser.mcp_server"]
    }
  }
}
```

This is a local MCP server, not a remote connector URL. See the [local MCP server setup guide](https://modelcontextprotocol.io/docs/develop/connect-local-servers).

Restart your chosen client after changing its settings, then start a new conversation.

### 5. Try your first request

Open the Chrome profile and window you want to use, then ask your agent:

> Use ai-browser to open https://example.com and briefly describe the page.

When opening the first page, the agent creates an **AI Browser** tab group or uses an available group with that name. If several windows match and the choice is unclear, specify the window you want.

## How tabs are selected

Each MCP connection is linked to its own Chrome profile, window, and **AI Browser** group. Active connections do not share a group. When a connection ends, its tabs stay open in Chrome and the group becomes available for reuse.

By default, the agent reads **only the selected tab**. Specify another tab when you want it read, or explicitly ask to read the whole group. Other tab groups are not connected automatically.

To let the agent work with a page you already have open, move that tab into its AI Browser group using Chrome, then ask the agent to select it. Moving a tab out of the group prevents new commands from accessing it. Content already included in the conversation remains in its history.

Ordinary interactions with page elements work in the background. Your activity on the same tab does not pause the agent, so editing the same form at the same time can interfere with the task.

## Make AI Browser the default in Codex

This step is optional. You can always include “use ai-browser” in your request instead.

To have Codex choose AI Browser for everyday requests to open a website or work on a page, install the skill from the project folder:

```sh
mkdir -p ~/.agents/skills
ln -s "$PWD/skills/ai-browser" ~/.agents/skills/ai-browser
```

If a skill with that name is already installed, check the existing folder or link first.

Add this rule to `~/.codex/AGENTS.md`:

```text
For browser actions, use the ai-browser MCP server and ai-browser skill
by default. An explicit user choice of another tool takes priority.
```

Start a new Codex session. You can then ask it to “open Chrome,” “go to example.com,” or “read the selected tab” without naming the tool. The rule guides the agent; the extension itself does not intercept text requests.

## Data access and permissions

The extension requests broad permissions for HTTP/HTTPS pages, browser actions, and Chrome debugger access. AI Browser's own checks restrict commands to the current connection's tab group. This is a restriction implemented by the program, not a separate browser sandbox.

The extension and local program communicate on your computer. When the agent reads a page, its text or screenshot enters the AI client and may be sent to the model provider under that service's policies. A local bridge does not mean that all AI processing happens locally.

Password values are excluded from element snapshots, and ordinary input tools reject password fields. Other visible page content can enter the agent's context. Sign in through Chrome yourself.

For an upload, the agent needs specific existing files and their absolute paths. A website may begin uploading as soon as a file is selected. Successful file selection does not confirm that the upload has finished.

System clicks based on a screenshot, such as clicks in Chrome's native date picker, require macOS **Accessibility** and **Screen Recording** permissions for the application that starts MCP. The target window and tab must be active and visible. Ordinary page interactions do not require these system permissions.

If the connection drops after a click or text entry and the outcome is unknown, the agent must check the page before repeating the action. The agent handles Chrome's unsaved-changes prompt based on the task; this does not guarantee that the website has saved your data.

## Updating

From the project folder:

```sh
git pull --ff-only
.venv/bin/python -m pip install -r requirements-mcp.lock
.venv/bin/python -m pip install --no-deps -e '.[mcp]'
```

Reload the extension at `chrome://extensions`. To reload the Python component, ask the agent to call `browser_restart`; your tabs stay open. If new tools are missing after an update, reconnect the MCP server or restart your AI client.

## Troubleshooting

| Problem | What to check |
| --- | --- |
| Your AI client cannot find AI Browser | Run `codex mcp list` or `claude mcp list`, check the Python path, and restart the client |
| The extension cannot connect | Check the extension ID used during installation, reload the extension, and read the message in its popup |
| The agent cannot see an open page | Make sure the tab is in this connection's group and explicitly select it |
| The window or profile is unclear | Ask the agent to list available connections and windows, then specify the one to use |
| A system click does not work | Check macOS permissions, make sure the target tab is active, and move any covering windows |
| Chrome shows a debugging banner | The debugger permission is used for input and observing browser dialogs |

The extension popup shows its connection status. For diagnostics on the MCP connection, ask the agent to call `browser_status`; `browser_connections` lists available Chrome connections.

## For developers

Tests require the installed MCP environment and Node.js:

```sh
BROWSER_BRIDGE_TEST=1 .venv/bin/python -m unittest discover -s tests -v
node --test tests/*.test.cjs
```

See the [technical reference in Russian](docs/technical-reference.ru.md) for tool details, connection recovery, and limitations. The [AI Browser skill](skills/ai-browser/SKILL.md) contains instructions for the agent.
