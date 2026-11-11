import { ChildProcess, execFile, spawn } from 'child_process';
import { promisify } from 'util';
import * as vscode from 'vscode';

const execFileAsync = promisify(execFile);

const CLI_TIMEOUT_MS = 60_000;
// Build and update can take many minutes on a large repository, so they
// run with no time limit (0 means none).
const NO_TIMEOUT = 0;
const INSTALL_TIMEOUT_MS = 120_000;
// How much of the end of watch's stderr to keep for an error message.
const WATCH_STDERR_TAIL = 2_000;

export interface CliResult {
    success: boolean;
    stdout: string;
    stderr: string;
}

export class CliWrapper {
    private readonly cliPath: string;
    private watchProcess: ChildProcess | undefined;

    constructor() {
        this.cliPath = this.getCliPath();
    }

    /**
     * Check whether the CLI binary is reachable.
     */
    async isInstalled(): Promise<boolean> {
        try {
            await execFileAsync(this.cliPath, ['--version'], { timeout: 10_000 });
            return true;
        } catch (err: unknown) {
            if (isEnoent(err)) {
                return false;
            }
            // Non-zero exit or other transient error — treat as not installed.
            return false;
        }
    }

    /**
     * Return the CLI version string, or undefined when the CLI is not available.
     */
    async getVersion(): Promise<string | undefined> {
        try {
            const { stdout } = await execFileAsync(this.cliPath, ['--version'], {
                timeout: 10_000,
            });
            return stdout.trim();
        } catch {
            return undefined;
        }
    }

    /**
     * Build (or fully rebuild) the graph database for a workspace.
     */
    async buildGraph(
        workspaceRoot: string,
        options?: { fullRebuild?: boolean },
    ): Promise<CliResult> {
        const args = ['build'];
        if (options?.fullRebuild) {
            args.push('--full');
        }

        return vscode.window.withProgress(
            {
                location: vscode.ProgressLocation.Notification,
                title: 'Code Review Graph: Building graph\u2026',
                cancellable: false,
            },
            () => this.exec(args, workspaceRoot, NO_TIMEOUT),
        );
    }

    /**
     * Incrementally update the graph database for a workspace.
     */
    async updateGraph(workspaceRoot: string): Promise<CliResult> {
        return this.exec(['update'], workspaceRoot, NO_TIMEOUT);
    }

    /** True while `code-review-graph watch` started by watchGraph() runs. */
    isWatching(): boolean {
        return this.watchProcess !== undefined;
    }

    /**
     * Run `code-review-graph watch` with no time limit. It keeps going until
     * stopWatch() is called or it exits by itself. The promise settles when
     * it ends; a stop asked for by stopWatch() counts as success.
     */
    watchGraph(workspaceRoot: string): Promise<CliResult> {
        return new Promise((resolve) => {
            // stdout is not read, so drop it: a full pipe would block watch.
            const child = spawn(this.cliPath, ['watch'], {
                cwd: workspaceRoot,
                stdio: ['ignore', 'ignore', 'pipe'],
            });
            this.watchProcess = child;

            let stderr = '';
            child.stderr?.setEncoding('utf8');
            child.stderr?.on('data', (chunk: string) => {
                stderr = (stderr + chunk).slice(-WATCH_STDERR_TAIL);
            });

            child.on('error', (err) => {
                if (this.watchProcess === child) {
                    this.watchProcess = undefined;
                }
                resolve(toCliResult(err));
            });

            child.on('close', (code) => {
                const stopped = this.watchProcess !== child;
                if (!stopped) {
                    this.watchProcess = undefined;
                }
                resolve({
                    success: stopped || code === 0,
                    stdout: '',
                    stderr: stderr.trim(),
                });
            });
        });
    }

    /** Stop the watch started by watchGraph(). Safe to call when none runs. */
    stopWatch(): void {
        const child = this.watchProcess;
        this.watchProcess = undefined;
        child?.kill();
    }

    /**
     * Compute embeddings for all graph nodes.
     */
    async embedGraph(workspaceRoot: string): Promise<CliResult> {
        return vscode.window.withProgress(
            {
                location: vscode.ProgressLocation.Notification,
                title: 'Code Review Graph: Computing embeddings\u2026',
                cancellable: false,
            },
            () => this.exec(['embed'], workspaceRoot),
        );
    }

    /**
     * Detect which Python package installer is available on the system.
     * Checks in preference order: uv, pipx, pip3.
     */
    async detectPythonInstaller(): Promise<'uv' | 'pipx' | 'pip' | null> {
        const candidates: Array<{ bin: string; result: 'uv' | 'pipx' | 'pip' }> = [
            { bin: 'uv', result: 'uv' },
            { bin: 'pipx', result: 'pipx' },
            { bin: 'pip3', result: 'pip' },
        ];

        for (const { bin, result } of candidates) {
            try {
                await execFileAsync(bin, ['--version'], { timeout: 10_000 });
                return result;
            } catch {
                // Not found or errored — try next.
            }
        }

        return null;
    }

    /**
     * Install the `code-review-graph` package using the specified installer.
     */
    async installBackend(installer: 'uv' | 'pipx' | 'pip'): Promise<CliResult> {
        const commandMap: Record<typeof installer, { bin: string; args: string[] }> = {
            uv: { bin: 'uv', args: ['pip', 'install', 'code-review-graph'] },
            pipx: { bin: 'pipx', args: ['install', 'code-review-graph'] },
            pip: { bin: 'pip3', args: ['install', 'code-review-graph'] },
        };

        const { bin, args } = commandMap[installer];

        return vscode.window.withProgress(
            {
                location: vscode.ProgressLocation.Notification,
                title: `Code Review Graph: Installing via ${installer}\u2026`,
                cancellable: false,
            },
            async () => {
                try {
                    const { stdout, stderr } = await execFileAsync(bin, args, {
                        timeout: INSTALL_TIMEOUT_MS,
                    });
                    return { success: true, stdout, stderr };
                } catch (err: unknown) {
                    return toCliResult(err);
                }
            },
        );
    }

    // ------------------------------------------------------------------ private

    private getCliPath(): string {
        const configured = vscode.workspace
            .getConfiguration('codeReviewGraph')
            .get<string>('cliPath', '');
        return configured || 'code-review-graph';
    }

    /**
     * Execute the CLI with the given arguments inside `cwd`.
     */
    private async exec(
        args: string[],
        cwd?: string,
        timeout: number = CLI_TIMEOUT_MS,
    ): Promise<CliResult> {
        try {
            const { stdout, stderr } = await execFileAsync(this.cliPath, args, {
                cwd,
                timeout,
            });
            return { success: true, stdout, stderr };
        } catch (err: unknown) {
            return toCliResult(err);
        }
    }
}

// --------------------------------------------------------------------- helpers

interface ExecError {
    code?: string | number;
    killed?: boolean;
    stdout?: string;
    stderr?: string;
    message?: string;
}

function isEnoent(err: unknown): boolean {
    return (err as ExecError)?.code === 'ENOENT';
}

function toCliResult(err: unknown): CliResult {
    const e = err as ExecError;

    if (isEnoent(err)) {
        return {
            success: false,
            stdout: '',
            stderr: 'CLI binary not found. Is code-review-graph installed?',
        };
    }

    if (e.killed) {
        return {
            success: false,
            stdout: e.stdout ?? '',
            stderr: 'Command timed out.',
        };
    }

    return {
        success: false,
        stdout: e.stdout ?? '',
        stderr: e.stderr ?? e.message ?? 'Unknown error',
    };
}
