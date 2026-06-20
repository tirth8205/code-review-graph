package com.example.advanced;

import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.context.annotation.Primary;
import org.springframework.stereotype.Service;

// ── Single-impl interface for implicit constructor injection ──
interface Mailer {
    void send(String to);
}

@Service
class SmtpMailer implements Mailer {
    public void send(String to) {}
}

// Implicit constructor injection (Spring 4.3+): one constructor, no @Autowired.
@Service
class WelcomeService {
    private final Mailer mailer;

    WelcomeService(Mailer mailer) {
        this.mailer = mailer;
    }

    public void welcome(String to) {
        mailer.send(to);
    }
}

// ── Multi-impl interface for @Qualifier / @Primary disambiguation ──
interface Gateway {
    String pay(String id);
}

@Service("primaryGw")
@Primary
class PrimaryGateway implements Gateway {
    public String pay(String id) { return "P:" + id; }
}

@Service("backupGw")
class BackupGateway implements Gateway {
    public String pay(String id) { return "B:" + id; }
}

// Field injection + @Qualifier → BackupGateway
@Service
class CheckoutService {
    @Autowired
    @Qualifier("backupGw")
    private Gateway gateway;

    public String checkout(String id) {
        return gateway.pay(id);
    }
}

// Constructor injection + @Qualifier on the parameter → PrimaryGateway
@Service
class RefundService {
    private final Gateway gateway;

    RefundService(@Qualifier("primaryGw") Gateway gateway) {
        this.gateway = gateway;
    }

    public String refund(String id) {
        return gateway.pay(id);
    }
}

// Field injection, no qualifier, multi-impl → @Primary (PrimaryGateway) wins
@Service
class AuditService {
    @Autowired
    private Gateway gateway;

    public String audit(String id) {
        return gateway.pay(id);
    }
}
