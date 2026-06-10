package com.example.temporal;

import io.temporal.workflow.WorkflowInterface;
import io.temporal.workflow.WorkflowMethod;
import io.temporal.workflow.SignalMethod;
import io.temporal.workflow.QueryMethod;
import io.temporal.activity.ActivityInterface;
import io.temporal.activity.ActivityMethod;

// ── Interfaces ───────────────────────────────────────────────────────────────

@WorkflowInterface
public interface OrderWorkflow {
    @WorkflowMethod
    String processOrder(String orderId);

    @SignalMethod
    void cancelOrder(String reason);

    @QueryMethod
    String getStatus();
}

@ActivityInterface
public interface PaymentActivity {
    @ActivityMethod
    boolean chargeCard(String orderId, double amount);
}

@ActivityInterface
public interface ShippingActivity {
    @ActivityMethod
    String shipOrder(String orderId);
}

// ── Implementations ──────────────────────────────────────────────────────────

// Workflow impl holds activity stubs as fields
class OrderWorkflowImpl implements OrderWorkflow {

    // These fields are assigned via Workflow.newActivityStub() at runtime
    private PaymentActivity paymentActivity;
    private ShippingActivity shippingActivity;

    // Static fields should NOT produce TEMPORAL_STUB edges
    private static final String TAG = "OrderWorkflowImpl";

    @Override
    public String processOrder(String orderId) {
        boolean paid = paymentActivity.chargeCard(orderId, 100.0);
        if (!paid) return "FAILED";
        String trackingId = shippingActivity.shipOrder(orderId);
        return trackingId;
    }

    @Override
    public void cancelOrder(String reason) {}

    @Override
    public String getStatus() { return "OK"; }
}

// Activity impls
class PaymentActivityImpl implements PaymentActivity {
    @Override
    public boolean chargeCard(String orderId, double amount) { return true; }
}

class ShippingActivityImpl implements ShippingActivity {
    @Override
    public String shipOrder(String orderId) { return "TRACK-001"; }
}
