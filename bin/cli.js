#!/usr/bin/env node

/**
 * Node.js wrapper for code-review-graph Python CLI.
 * This forwards all commands and arguments to the underlying Python implementation.
 */

const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');

function findPythonCLI() {
  // 1. Check if the command is already in PATH (e.g. installed via global pip)
  // We don't use 'which' here to avoid platform issues, instead we try to spawn it.
  
  // 2. Check for a local virtualenv in the package directory
  // This is where postinstall might have installed it
  const packageRoot = path.join(__dirname, '..');
  const venvBin = process.platform === 'win32' 
    ? path.join(packageRoot, '.venv', 'Scripts', 'code-review-graph.exe')
    : path.join(packageRoot, '.venv', 'bin', 'code-review-graph');

  if (fs.existsSync(venvBin)) {
    return venvBin;
  }

  // 3. Fallback to just 'code-review-graph' assuming it's in PATH
  return 'code-review-graph';
}

const cliPath = findPythonCLI();
const args = process.argv.slice(2);

// Handle the case where the CLI isn't found at all
// We'll try to run it; if it fails with ENOENT, we'll give a helpful message.

const child = spawn(cliPath, args, {
  stdio: 'inherit',
  env: {
    ...process.env,
    // Ensure Python doesn't buffer output, which is critical for MCP server mode
    PYTHONUNBUFFERED: '1'
  }
});

child.on('error', (err) => {
  if (err.code === 'ENOENT') {
    console.error('\x1b[31mError: code-review-graph Python core not found.\x1b[0m');
    console.error('Please ensure you have Python 3.10+ installed.');
    console.error('Try running: npm install (to trigger postinstall setup)');
    console.error('\nIf that fails, you can install the core manually via:');
    console.error('  pip install code-review-graph');
    process.exit(1);
  } else {
    console.error('Error spawning code-review-graph:', err.message);
    process.exit(1);
  }
});

child.on('exit', (code, signal) => {
  if (code !== null) {
    process.exit(code);
  } else if (signal) {
    process.kill(process.pid, signal);
  }
});

// Forward signals to the child process
const signals = ['SIGINT', 'SIGTERM', 'SIGHUP'];
signals.forEach(sig => {
  process.on(sig, () => {
    if (child.connected) {
      child.kill(sig);
    }
  });
});
