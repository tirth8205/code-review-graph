package com.example.web;

import javax.servlet.http.HttpServlet;
import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;

public class UserServlet extends HttpServlet {
    @Override
    protected void doGet(HttpServletRequest req, HttpServletResponse resp) {
        String userId = req.getParameter("id");
        handleGetUser(userId, resp);
    }

    @Override
    protected void doPost(HttpServletRequest req, HttpServletResponse resp) {
        handleCreateUser(req, resp);
    }

    private void handleGetUser(String id, HttpServletResponse resp) {
        resp.setStatus(200);
    }

    private void handleCreateUser(HttpServletRequest req, HttpServletResponse resp) {
        resp.setStatus(201);
    }
}
