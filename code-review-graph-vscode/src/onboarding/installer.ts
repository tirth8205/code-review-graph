import * as vscode from 'vscode';
import { CliWrapper } from '../backend/cli';

/**
 * Handles auto-detection and installation of the Python backend.
 *
 * Checks whether the `code-review-graph` CLI is available and, if not,
 * guides the user through installation via pip/pipx or manual instructions.
 */
export class Installer {
    constructor(private cli: CliWrapper) {}

    /**
     * Check whether the backend is installed and prompt the user if it is not.
     *
     * @returns `true` if the backend is installed (or was just installed
     *          successfully), `false` if the user dismissed the prompt or
     *          installation failed.
     */
    async checkAndPrompt(): Promise<boolean> {
        const installed = await this.cli.isInstalled();
        if (installed) {
            return true;
        }

        const selection = await vscode.window.showInformationMessage(
            'Code Review Graph backend is not installed.',
            'Install Now',
            'Manual Instructions',
            'Dismiss',
        );

        if (selection === 'Install Now') {
            return this.autoInstall();
        }

        if (selection === 'Manual Instructions') {
            const terminal = vscode.window.createTerminal('Code Review Graph Setup');
            terminal.show();
            terminal.sendText('echo "=== Code Review Graph - Manual Installation ==="');
            terminal.sendText('echo ""');
            terminal.sendText('echo "Option 1: Install with pip"');
            terminal.sendText('echo "  pip install code-review-graph"');
            terminal.sendText('echo ""');
            terminal.sendText('echo "Option 2: Install with pipx (recommended)"');
            terminal.sendText('echo "  pipx install code-review-graph"');
            terminal.sendText('echo ""');
            terminal.sendText('echo "After installation, reload the VS Code window."');
            return false;
        }

        // Dismissed
        return false;
    }

    /**
     * Attempt to automatically install the backend using the first available
     * Python package installer (pip or pipx).
     *
     * @returns `true` if installation succeeded, `false` otherwise.
     */
    async autoInstall(): Promise<boolean> {
        const installer = await this.cli.detectPythonInstaller();

        if (!installer) {
            const openLink = 'Download Python';
            const response = await vscode.window.showErrorMessage(
                'Python 3.12+ is required. Install Python first.',
                openLink,
            );

            if (response === openLink) {
                vscode.env.openExternal(
                    vscode.Uri.parse('https://www.python.org/downloads/'),
                );
            }

            return false;
        }

        let success = false;

        await vscode.window.withProgress(
            {
                location: vscode.ProgressLocation.Notification,
                title: `Installing code-review-graph via ${installer}...`,
                cancellable: false,
            },
            async () => {
                try {
                    await this.cli.installBackend(installer);
                    success = true;
                } catch {
                    success = false;
                }
            },
        );

        if (success) {
            vscode.window.showInformationMessage(
                'Backend installed successfully!',
            );
            return true;
        }

        vscode.window.showErrorMessage(
            `Failed to install code-review-graph via ${installer}. ` +
            'Check the terminal output for details or try installing manually: ' +
            `\`${installer} install code-review-graph\``,
        );

        return false;
    }
}
