#pragma once
#include <QMainWindow>

QT_BEGIN_NAMESPACE namespace Ui { class MyWidgetClass; };
QT_END_NAMESPACE

class MyWidget : public QMainWindow {
  Q_OBJECT

 public:
  MyWidget(QWidget* parent = nullptr);
  ~MyWidget();
  void doSomething();
  int calculateValue(int a, int b);

 protected Q_SLOTS:
  void onButtonClicked();
  void onDataReceived(int value);

 public Q_SLOTS:
  void onReset();

 Q_SIGNALS:
  void dataReady(int result);
  void errorOccurred(const QString& msg);
};
