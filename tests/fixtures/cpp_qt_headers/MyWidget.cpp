#include "MyWidget.h"

MyWidget::MyWidget(QWidget* parent) : QMainWindow(parent) {}
MyWidget::~MyWidget() {}
void MyWidget::doSomething() { onReset(); }
int MyWidget::calculateValue(int a, int b) { return a + b; }
void MyWidget::onButtonClicked() { Q_EMIT dataReady(calculateValue(1, 2)); }
void MyWidget::onDataReceived(int value) {
  if (value < 0) { Q_EMIT errorOccurred("err"); return; }
  doSomething();
}
void MyWidget::onReset() { Q_EMIT dataReady(0); }
