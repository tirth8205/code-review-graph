#pragma once

class MyWidgetPlain {
 public:
  MyWidgetPlain();
  ~MyWidgetPlain();
  void doSomething();
  int calculateValue(int a, int b);

 protected:
  void onButtonClicked();
  void onDataReceived(int value);
  void onReset();
};
