/**
 * Fixture: cross-file TypeScript import with named + default imports.
 *
 * Pain point: when two files define the same function name (e.g. `validate`),
 * the parser should use import_map to disambiguate.
 */

import { UserService } from './sample_typescript';
import type { User } from './sample_typescript';

export function handleRequest(id: number): void {
    const svc = new UserService();
    const user = svc.findById(id);
    if (user) {
        console.log(user);
    }
}
