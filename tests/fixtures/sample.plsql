-- Oracle PL/SQL fixture covering common real-world Oracle code patterns.
-- Some files start directly with the object keyword (no CREATE OR REPLACE prefix).

--------------------------------------------------------------------------------
-- Package specification: PACKAGE name ... IS
--------------------------------------------------------------------------------
PACKAGE                           HR_PKG
--------------------------------------------------------------------------------
-- PACKAGE DESCRIPTION: Human resources utilities.
--------------------------------------------------------------------------------
IS
  PROCEDURE hire_employee(p_name VARCHAR2, p_dept NUMBER, pio_Err IN OUT SrvErr);
  FUNCTION  get_salary(p_id NUMBER) RETURN NUMBER;
END HR_PKG;
/

--------------------------------------------------------------------------------
-- Package body: PACKAGE BODY name AS
--------------------------------------------------------------------------------
PACKAGE BODY HR_PKG AS

PROCEDURE hire_employee(p_name VARCHAR2, p_dept NUMBER, pio_Err IN OUT SrvErr) IS
BEGIN
    INSERT INTO employees (emp_name, dept_id) VALUES (p_name, p_dept);
    AUDIT_PKG.log_change('HIRE', p_name);
END hire_employee;

FUNCTION get_salary(p_id NUMBER) RETURN NUMBER IS
    v_sal NUMBER;
BEGIN
    SELECT salary INTO v_sal FROM employees WHERE emp_id = p_id;
    NOTIF_PKG.send_alert(p_id, v_sal);
    RETURN v_sal;
END get_salary;

END HR_PKG;
/

--------------------------------------------------------------------------------
-- Trigger: schema-qualified name and table (TRIGGER schema.name BEFORE ... ON schema.table)
--------------------------------------------------------------------------------
TRIGGER CURRENCIES.AUDIT_EMP_TRG
 BEFORE
 INSERT OR UPDATE
 ON CURRENCIES.EMPLOYEES
 REFERENCING OLD AS OLD NEW AS NEW
 FOR EACH ROW
BEGIN
    :new.updated_at := SYSDATE;
END;
/

--------------------------------------------------------------------------------
-- Standalone procedure
--------------------------------------------------------------------------------
PROCEDURE standalone_proc(p_id IN NUMBER) IS
BEGIN
    UPDATE employees SET active = 0 WHERE emp_id = p_id;
END;
/

--------------------------------------------------------------------------------
-- Standalone function (with CREATE OR REPLACE — some files use this form)
--------------------------------------------------------------------------------
create or replace PROCEDURE HR_SCHEMA.standalone_func_alt(pi_msg VARCHAR2) IS
BEGIN NULL; END;
/

FUNCTION standalone_func(p_id IN NUMBER) RETURN VARCHAR2 AS
    v_name VARCHAR2(100);
BEGIN
    SELECT emp_name INTO v_name FROM employees WHERE emp_id = p_id;
    RETURN v_name;
END;
/

--------------------------------------------------------------------------------
-- Type definition
--------------------------------------------------------------------------------
TYPE                 "ADDRESS_T"                                          FORCE AS OBJECT(
    street   VARCHAR2(100),
    city     VARCHAR2(50),
    MEMBER FUNCTION to_string RETURN VARCHAR2
);
/
