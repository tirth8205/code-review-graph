-- Sample Oracle PL/SQL fixture for code-review-graph parser tests

CREATE TABLE employees (
    id       NUMBER PRIMARY KEY,
    name     VARCHAR2(100) NOT NULL,
    salary   NUMBER(10, 2)
);

CREATE OR REPLACE PROCEDURE give_raise (
    p_emp_id IN employees.id%TYPE,
    p_amount IN NUMBER
) IS
BEGIN
    UPDATE employees
    SET salary = salary + p_amount
    WHERE id = p_emp_id;

    log_salary_change(p_emp_id, p_amount);
    COMMIT;
END give_raise;
/

CREATE OR REPLACE FUNCTION get_salary (
    p_emp_id IN employees.id%TYPE
) RETURN NUMBER IS
    v_salary NUMBER(10, 2);
BEGIN
    SELECT salary INTO v_salary FROM employees WHERE id = p_emp_id;
    RETURN v_salary;
END get_salary;
/

CREATE OR REPLACE TRIGGER trg_employees_audit
AFTER UPDATE ON employees
FOR EACH ROW
BEGIN
    audit_pkg.log_change('employees', :OLD.id);
END trg_employees_audit;
/

CREATE OR REPLACE PACKAGE payroll_pkg IS
    PROCEDURE process_payroll(p_emp_id IN NUMBER);
    FUNCTION calculate_bonus(p_emp_id IN NUMBER) RETURN NUMBER;
END payroll_pkg;
/

CREATE OR REPLACE PACKAGE BODY payroll_pkg IS

    FUNCTION calculate_bonus(p_emp_id IN NUMBER) RETURN NUMBER IS
        v_bonus NUMBER(10, 2);
    BEGIN
        v_bonus := get_salary(p_emp_id) * 0.1;
        RETURN v_bonus;
    END calculate_bonus;

    PROCEDURE process_payroll(p_emp_id IN NUMBER) IS
        v_bonus NUMBER(10, 2);
    BEGIN
        v_bonus := calculate_bonus(p_emp_id);
        give_raise(p_emp_id, v_bonus);
    END process_payroll;

END payroll_pkg;
/
