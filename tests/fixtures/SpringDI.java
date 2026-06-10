package com.example.shop;

import org.springframework.stereotype.Service;
import org.springframework.stereotype.Repository;
import org.springframework.stereotype.Component;
import org.springframework.stereotype.Controller;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import lombok.RequiredArgsConstructor;

// Plain interface — not a Spring bean
public interface OrderRepository {
    void save(Order order);
    Order findById(Long id);
}

// @Repository stereotype — Spring-managed bean
@Repository
class JpaOrderRepository implements OrderRepository {
    @Override
    public void save(Order order) {}

    @Override
    public Order findById(Long id) { return null; }
}

// @Service with @Autowired field injection
@Service
class NotificationService {
    @Autowired
    private OrderRepository orderRepository;

    public void notify(Long orderId) {
        Order o = orderRepository.findById(orderId);
    }
}

// @Service with Lombok @RequiredArgsConstructor (constructor injection via final fields)
@Service
@RequiredArgsConstructor
class OrderService {
    private final OrderRepository orderRepository;
    private final NotificationService notificationService;
    private static final String TAG = "OrderService";  // static final — NOT injected

    public void placeOrder(Order order) {
        orderRepository.save(order);
        notificationService.notify(order.getId());
    }
}

// @Component with explicit @Autowired constructor
@Component
class AuditLogger {
    private final OrderRepository orderRepository;

    @Autowired
    public AuditLogger(OrderRepository orderRepository) {
        this.orderRepository = orderRepository;
    }

    public void log(String msg) {}
}

// @Configuration with @Bean factory methods
@Configuration
class AppConfig {
    @Bean
    public OrderRepository orderRepository() {
        return new JpaOrderRepository();
    }
}

class Order {
    private Long id;
    public Long getId() { return id; }
}
