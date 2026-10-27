/**
 * @fileoverview Segmented control for choosing the reader level.
 */

import {LEVEL_LABELS, type Level} from '../api';

const LEVELS = Object.keys(LEVEL_LABELS) as Level[];

interface LevelPickerProps {
  value: Level;
  onChange: (level: Level) => void;
}

/** Lets the user pick who the guide should be written for. */
export function LevelPicker({value, onChange}: LevelPickerProps) {
  return (
    <fieldset className="mt-4">
      <legend className="mb-2 block text-sm font-semibold">
        누구를 위한 설명인가요?
      </legend>
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
        {LEVELS.map(level => (
          <label
            key={level}
            className={
              'flex h-11 cursor-pointer items-center justify-center ' +
              'rounded-lg border text-sm transition ' +
              (value === level
                ? 'border-indigo-500 bg-indigo-50 font-medium text-indigo-700'
                : 'border-slate-300 text-slate-600 hover:border-slate-400')
            }
          >
            <input
              type="radio"
              name="level"
              value={level}
              checked={value === level}
              onChange={() => onChange(level)}
              className="sr-only"
            />
            {LEVEL_LABELS[level]}
          </label>
        ))}
      </div>
    </fieldset>
  );
}
