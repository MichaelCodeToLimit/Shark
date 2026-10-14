// Shark extension for VS Code.
//
// Syntax highlighting is declarative and works with no dependencies at all.
// Completion, diagnostics, hover and go-to-definition come from the language
// server in editor/shark_lsp.py, which needs `npm install` in this folder for
// the `vscode-languageclient` package. If that package is missing the
// extension still loads and highlighting still works.

const fs = require('fs');
const path = require('path');
const vscode = require('vscode');

let client;

function findPython(configured) {
  if (configured) {
    return configured;
  }
  return process.platform === 'win32' ? 'python' : 'python3';
}

function findServer(context, configured) {
  if (configured) {
    return configured;
  }
  // Bundled copy, if the extension was packaged with one.
  const bundled = context.asAbsolutePath(path.join('server', 'shark_lsp.py'));
  if (fs.existsSync(bundled)) {
    return bundled;
  }
  // Otherwise look for a Shark checkout in the open workspace.
  for (const folder of vscode.workspace.workspaceFolders || []) {
    const candidate = path.join(folder.uri.fsPath, 'editor', 'shark_lsp.py');
    if (fs.existsSync(candidate)) {
      return candidate;
    }
  }
  return null;
}

function activate(context) {
  const config = vscode.workspace.getConfiguration('shark');
  if (config.get('server.enabled') === false) {
    return;
  }

  let node;
  try {
    node = require('vscode-languageclient/node');
  } catch (err) {
    vscode.window.showInformationMessage(
      'Shark: highlighting is active. For completion and diagnostics, run ' +
      '"npm install" in the extension folder and reload the window.');
    return;
  }

  const server = findServer(context, config.get('server.path'));
  if (!server) {
    vscode.window.showWarningMessage(
      'Shark: could not find shark_lsp.py. Set "shark.server.path" to the ' +
      'editor/shark_lsp.py in your Shark checkout.');
    return;
  }

  const python = findPython(config.get('python.path'));
  const run = { command: python, args: [server], transport: node.TransportKind.stdio };

  client = new node.LanguageClient(
    'shark',
    'Shark',
    { run: run, debug: run },
    {
      documentSelector: [
        { scheme: 'file', language: 'shark' },
        { scheme: 'untitled', language: 'shark' }
      ],
      outputChannelName: 'Shark',
      synchronize: {
        fileEvents: vscode.workspace.createFileSystemWatcher(
          '**/registry/**/*.{signatures,locale}')
      }
    }
  );

  client.start().catch(function (err) {
    vscode.window.showErrorMessage(
      'Shark language server failed to start: ' + err.message +
      '. Check that "' + python + '" is Python 3.8 or newer.');
  });

  context.subscriptions.push(
    vscode.commands.registerCommand('shark.restartServer', async function () {
      if (client) {
        await client.restart();
        vscode.window.showInformationMessage('Shark language server restarted.');
      }
    })
  );
}

function deactivate() {
  return client ? client.stop() : undefined;
}

module.exports = { activate, deactivate };
