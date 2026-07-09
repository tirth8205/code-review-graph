import React from 'react';
import styles from './button.module.css';

function Button({ label }: { label: string }) {
  return (
    <button className="btn btn-primary" onClick={() => {}}>
      {label}
    </button>
  );
}

function StyledButton({ label }: { label: string }) {
  return (
    <button className={styles.btnOutline}>
      {label}
    </button>
  );
}

function DynamicButton({ active }: { active: boolean }) {
  return (
    <button className={active ? "active" : "inactive"}>
      Toggle
    </button>
  );
}

export { Button, StyledButton, DynamicButton };
