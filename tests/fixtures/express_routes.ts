/**
 * Fixture: Express.js route handlers.
 * These should be detected as framework entry points.
 */

import express from 'express';

const app = express();

function getUsers(req: any, res: any) {
    res.json([]);
}

function createUser(req: any, res: any) {
    res.status(201).json({ id: 1 });
}

function errorHandler(err: any, req: any, res: any, next: any) {
    res.status(500).json({ error: err.message });
}

app.get('/users', getUsers);
app.post('/users', createUser);
app.use(errorHandler);
