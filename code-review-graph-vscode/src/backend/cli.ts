import { execFile } from 'child_process';
import { promisify } from 'util';
import * as vscode from 'vscode';

const execFileAsync = promisify(execFile);

const CLI_TIMEOUT_MS = 60_000;
const INSTALL_TIMEOUT_MS = 120_000;

export interface CliResult {
    success: boolean;
    stdout: string;
    stderr: string;
}

export class CliWrapper {
    private readonly cliPath: string;

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
    async buildGraph(workspaceRoot: string): Promise<CliResult> {
        return vscode.window.withProgress(
            {
                location: vscode.ProgressLocation.Notification,
                title: 'Code Review Graph: Building graph\u2026',
                cancellable: false,
            },
            () => this.exec(['build'], workspaceRoot),
        );
    }

    /**
     * Incrementally update the graph database for a workspace.
     */
    async updateGraph(workspaceRoot: string): Promise<CliResult> {
        return this.exec(['update'], workspaceRoot);
    }

    /**
     * Start the watch daemon for continuous file monitoring.
     */
    async watchGraph(workspaceRoot: string): Promise<CliResult> {
        return this.exec(['watch'], workspaceRoot);
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
    private async exec(args: string[], cwd?: string): Promise<CliResult> {
        try {
            const { stdout, stderr } = await execFileAsync(this.cliPath, args, {
                cwd,
                timeout: CLI_TIMEOUT_MS,
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
