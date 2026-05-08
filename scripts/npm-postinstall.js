/**
 * NPM Post-install script for code-review-graph.
 * Sets up the Python environment and installs the core package.
 */

const { execSync, spawnSync } = require('child_process');
const path = require('path');
const fs = require('fs');

const packageRoot = path.join(__dirname, '..');
const venvDir = path.join(packageRoot, '.venv');

console.log('\x1b[36mSetting up code-review-graph Python core...\x1b[0m');

function run(cmd, args, options = {}) {
  const result = spawnSync(cmd, args, {
    stdio: 'inherit',
    shell: true,
    cwd: packageRoot,
    ...options
  });
  if (result.status !== 0) {
    throw new Error(`Command failed: ${cmd} ${args.join(' ')}`);
  }
}

try {
  // 1. Check for Python 3.10+
  try {
    const pyVersion = execSync('python3 --version').toString().trim();
    console.log(`Found ${pyVersion}`);
  } catch (e) {
    console.error('\x1b[31mError: Python 3 not found.\x1b[0m');
    console.error('code-review-graph requires Python 3.10 or higher.');
    process.exit(1);
  }

  // 2. Check for uv (faster) or fallback to venv + pip
  let hasUv = false;
  try {
    execSync('uv --version');
    hasUv = true;
    console.log('Found uv, using it for faster setup...');
  } catch (e) {}

  if (hasUv) {
    run('uv', ['sync', '--no-dev']);
  } else {
    // Standard venv + pip
    if (!fs.existsSync(venvDir)) {
      console.log('Creating virtual environment...');
      run('python3', ['-m', 'venv', '.venv']);
    }

    const pipPath = process.platform === 'win32'
      ? path.join(venvDir, 'Scripts', 'pip')
      : path.join(venvDir, 'bin', 'pip');

    console.log('Installing Python dependencies...');
    run(pipPath, ['install', '--upgrade', 'pip']);
    run(pipPath, ['install', '.']);
  }

  console.log('\x1b[32m\nSuccessfully set up code-review-graph!\x1b[0m');
  console.log('You can now run it using: code-review-graph --help');

} catch (error) {
  console.error('\x1b[31m\nFailed to set up Python environment.\x1b[0m');
  console.error(error.message);
  console.error('\nYou can still use the tool if you install it manually via pip:');
  console.error('  pip install code-review-graph');
}
