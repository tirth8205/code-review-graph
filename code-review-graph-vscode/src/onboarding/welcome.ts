import * as vscode from 'vscode';
import { Installer } from './installer';
import { CliWrapper } from '../backend/cli';

/**
 * Register command handlers for the walkthrough steps defined in
 * `package.json` contributes.walkthroughs.
 *
 * Each walkthrough step button triggers one of these commands so the user
 * can install the CLI, build the graph, and explore the sidebar without
 * leaving the walkthrough.
 */
export function registerWalkthroughCommands(
    context: vscode.ExtensionContext,
    cli: CliWrapper,
    installer: Installer,
): void {
    context.subscriptions.push(
        vscode.commands.registerCommand(
            'codeReviewGraph.walkthrough.install',
            async () => {
                await installer.autoInstall();
            },
        ),
    );

    context.subscriptions.push(
        vscode.commands.registerCommand(
            'codeReviewGraph.walkthrough.build',
            async () => {
                const workspaceFolder = vscode.workspace.workspaceFolders?.[0];
                if (!workspaceFolder) {
                    vscode.window.showWarningMessage(
                        'No workspace folder is open. Open a folder first.',
                    );
                    return;
                }

                // buildGraph shows its own progress notification.
                const result = await cli.buildGraph(workspaceFolder.uri.fsPath);
                if (result.success) {
                    vscode.window.showInformationMessage(
                        'Code Graph: Build complete.',
                    );
                } else {
                    vscode.window.showErrorMessage(
                        `Code Graph: Build failed. ${result.stderr}`,
                    );
                }
            },
        ),
    );

    context.subscriptions.push(
        vscode.commands.registerCommand(
            'codeReviewGraph.walkthrough.explore',
            async () => {
                await vscode.commands.executeCommand(
                    'codeReviewGraph.codeGraph.focus',
                );
            },
        ),
    );
}

/**
 * Show a welcome notification if no graph database has been built yet
 * in any of the open workspace folders.
 *
 * Checks for `.code-review-graph/graph.db` in every workspace folder.
 * When none is found, a notification is shown with a button that opens
 * the built-in walkthrough.
 */
export async function showWelcomeIfNeeded(
    context: vscode.ExtensionContext,
): Promise<void> {
    const workspaceFolders = vscode.workspace.workspaceFolders;
    if (!workspaceFolders || workspaceFolders.length === 0) {
        return;
    }

    for (const folder of workspaceFolders) {
        const dbUri = vscode.Uri.joinPath(
            folder.uri,
            '.code-review-graph',
            'graph.db',
        );

        try {
            await vscode.workspace.fs.stat(dbUri);
            // Database exists in at least one folder -- no need to prompt.
            return;
        } catch {
            // File does not exist in this folder -- continue checking.
        }
    }

    // No graph.db found in any workspace folder.
    const selection = await vscode.window.showInformationMessage(
        'Welcome to Code Review Graph! Get started by building your code graph.',
        'Get Started',
    );

    if (selection === 'Get Started') {
        await vscode.commands.executeCommand(
            'workbench.action.openWalkthrough',
            'tirth8205.code-review-graph#codeReviewGraph.welcome',
        );
    }
}
