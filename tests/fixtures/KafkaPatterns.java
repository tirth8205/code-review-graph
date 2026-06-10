package com.example.kafka;

import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.kafka.annotation.KafkaHandler;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.kafka.core.KafkaOperations;
import org.springframework.stereotype.Service;
import org.springframework.stereotype.Component;
import lombok.RequiredArgsConstructor;
import reactor.kafka.receiver.KafkaReceiver;

// ── Annotation-based consumer ─────────────────────────────────────────────

@Service
class OrderEventConsumer {

    @KafkaListener(topics = "order-events")
    public void onOrder(String payload) {}

    @KafkaListener(topics = {"order-dlq", "order-retry"})
    public void onDlq(String payload) {}
}

// ── Annotation-based producer (KafkaTemplate field) ───────────────────────

@Service
@RequiredArgsConstructor
class NotificationProducer {
    private final KafkaTemplate<String, String> kafkaTemplate;
    // static field — should NOT produce edge
    private static final String TOPIC = "notifications";
}

// ── Reactive consumer (KafkaReceiver field) ───────────────────────────────

@Service
@RequiredArgsConstructor
class ReactiveOrderConsumer {
    private final KafkaReceiver<String, OrderEvent> kafkaReceiver;
    private final KafkaOperations<String, String> kafkaOps;
}

// ── plain class with no Kafka ─────────────────────────────────────────────

class OrderEvent {
    private String id;
}
