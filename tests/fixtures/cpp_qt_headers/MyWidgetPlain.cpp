#include "MyWidgetPlain.h"

MyWidgetPlain::MyWidgetPlain() {}
MyWidgetPlain::~MyWidgetPlain() {}

void MyWidgetPlain::doSomething() { onReset(); }
int MyWidgetPlain::calculateValue(int a, int b) { return a + b; }
void MyWidgetPlain::onButtonClicked() { int result = calculateValue(1, 2); }
void MyWidgetPlain::onDataReceived(int value) { if (value < 0) return; doSomething(); }
void MyWidgetPlain::onReset() {}
