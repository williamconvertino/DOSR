# SWE-bench failing patches: ground-truth failure reasons

Patches come from the leaderboard run `20241108_autocoderover-v2.0-claude-3-5-sonnet-20241022` (SWE-bench Verified).
Sources: the official `test_output.txt` / `report.json` for each instance (same S3 prefix as `source.report`), the failing `candidate.diff` and the upstream `<...>-gold/candidate.diff`.
These reasons are the reference for grading diagnosis in `grades-swe.csv` (`found_target`). All grades are Claude suggestions.

Two instances cannot be decided from the test log, because the test module never ran:
- **astropy-8872**: the module failed to collect because of an environment problem.
- **pylint-4604**: the tests import a constant that only the gold patch adds.

Four other leaderboard runs fail these two instances the same way (OpenHands-2.1, Agentless-1.5, devlo, wandb-o1). For these two, the grading uses the real semantic difference from gold.

---

### final-swe-astropy-astropy-8872-fail: `broke_other_code` (official label)

**Reason.** The official failure is an environment artifact and not caused by the patch. `test_quantity.py` fails at collection: line 29 calls `LooseVersion(matplotlib.__version__)`, which raises a `DeprecationWarning`, and the warning is promoted to an error. That is why every F2P and P2P test (1 + 80) is reported failed. Judged against gold, the patch would pass `test_preserve_dtype`, since float16 and float32 are kept and int and Decimal are still cast. Its real defect is the new condition `value.dtype.kind not in 'f' or value.dtype.fields is not None or kind == 'O'`:
- It now casts **complex** inputs (kind `c`) to float, which silently drops the imaginary part.
- It casts **structured** dtypes, which the old code explicitly exempted.

Gold casts only `kind in 'iuO'`, so it keeps both complex and structured dtypes. Gold also updates the Quantity-input copy path, which the patch leaves alone.

**Evidence.** `ERROR astropy/units/tests/test_quantity.py - DeprecationWarning: distutils Version classes are deprecated`, then `Interrupted: 2 errors during collection`. `report.json` shows F2P 1 fail and P2P 80 fail with 0 success. The other four runs listed above have identical results.

**Grading rule.** y = names the complex-dtype or structured-dtype regression.

### final-swe-django-django-11433-fail: `broke_other_code`

**Reason.** The added guard `f.name not in cleaned_data` comes after the loop's earlier `... or f.name not in cleaned_data: continue`, so it is always False at that point. The "keep model default for omitted field" branch therefore never runs. Every defaulted field that is missing from POST data is now overwritten with its cleaned empty value (`''`, `None`, `[]`, ...) instead of keeping the model default. Gold keeps the omitted-field skip and lets `cleaned_data` win only when its value is not in `field.empty_values`.

**Evidence.**
- `test_default_populated_on_optional_field`: `AssertionError: '' != 'di'`
- `test_default_selectdatewidget`: `None != datetime.date(...)`
- `test_default_splitdatetime_field`: `None != datetime.datetime(2000, 1, 1, 0, 0)`
- The F2P `test_default_not_populated_on_non_empty_value_in_cleaned_data` fails for each empty_value subtest (`None != 'di'`, `'' != 'di'`, ...).

**Grading rule.** y = says that default preservation is effectively removed, so omitted fields are blanked. A review that sees the dead condition but concludes the patch is a **no-op** gets the effect backwards and is graded n (marked BORDERLINE).

### final-swe-django-django-11820-fail: `did_not_fix`

**Reason.** The patch strips a trailing `pk` from the lookup before validation. It does not fix the traversal in `_check_ordering`: after a **non-relational** field, `_cls` stays on the previous model, so later parts are looked up on the wrong model. As a result `ordering = ('parent__field1__field2',)` passes silently, although it should raise models.E015. Gold resolves `pk` to `_meta.pk` at any position and sets `_cls = None` after a non-relational field.

**Evidence.** `test_ordering_pointing_multiple_times_to_model_fields`: `Lists differ: [] != [<Error ... 'ordering' refers to the nonexistent field, related field, or lookup 'parent__field1__field2' ... id='models.E015'>]`.

**Grading rule.** Every review flagged that the check becomes over-permissive for `<non-relational>__pk`. That is the same class of hole (lookups continuing past a non-relational field are not rejected) but not the exact path the test uses. Graded y, marked BORDERLINE.

### final-swe-matplotlib-matplotlib-24570-fail: `broke_other_code`

**Reason.** The issue is that `top` and `bottom` are swapped in `_get_aligned_offsets`. Gold fixes it by swapping those branches. The patch instead changes HPacker's input from `(h, yd)` to `(h, h - yd)`, which corrupts the descent for **every** alignment:
- `top` and `bottom` still give wrong child offsets, outside the box.
- `baseline` and `center` change too, which breaks extents and offsets.

**Evidence.**
- `test_packers[bottom]` and `test_packers[top]` fail.
- The P2P tests `test_packers[baseline|left|right|center]` also fail.
- Example extent mismatch: `x: array([30, 60, 0, 0])`, `y: array([30., 60., 0., 60.])`. Example offset mismatch: `[[0,0],[10,0]]` vs `[[0,60],[10,60]]`.

### final-swe-psf-requests-5414-fail: `did_not_fix`

**Reason.** The patch only changes `_get_idna_encoded_host` to raise `InvalidURL`. `prepare_url` calls that function only for **non-ASCII** hosts, and `.example.com` is ASCII, so it never reaches the changed code. The host passes through and urllib3 fails later. Gold adds a leading-`.` check next to the existing leading-`*` check.

**Evidence.** `test_invalid_url[InvalidURL-http://.example.com]` fails with `urllib3.exceptions.LocationParseError: Failed to parse: '.example.com', label empty or too long`, raised from `adapter.send` instead of `InvalidURL`.

### final-swe-pydata-xarray-6744-fail: `broke_other_code`

**Reason.** For `center=True` the patch **subtracts** `(w-1)//2` from both starts and stops. That moves the trailing windows further back (for example, the first window becomes `[0:0]`) instead of forward to centre them. The manually iterated windows therefore do not match `rolling(center=True)` reductions. Gold uses `offset = (w+1)//2` and builds `stops = arange(offset, n+offset)`.

**Evidence.** `test_rolling_iter[numpy|dask-3|7-True-1|2]` fail (3 F2P and 5 P2P) with `x and y nan location mismatch`. The iterated results are values where rolling gives NaN, and the reverse.

### final-swe-pylint-dev-pylint-4604-fail: `did_not_fix` (no graded rows in these runs)

**Reason.** The official failure is a test-coupling artifact. The updated `unittest_variables.py` does `from pylint.constants import IS_PYPY`, and only the gold patch adds that constant, so all 21 tests fail with an ImportError at collection. Semantically, the patch handles `astroid.Attribute` annotations only when `.expr` is a plain `Name`: it appends `expr.name` and returns. Gold recurses with `_store_type_annotation_node(type_annotation.expr)`, so the patch misses multi-level dotted annotations such as `a.b.C`.

**Evidence.** `ImportError: cannot import name 'IS_PYPY' from 'pylint.constants'`, then `ERROR tests/checkers/unittest_variables.py`. The other four runs listed above have the same 21 F2P failures.

### final-swe-pytest-dev-pytest-10051-fail: `did_not_fix`

**Reason.** `caplog.clear()` calls `handler.reset()`, which binds a new list. The patch then sets the stash entry of **every** phase (`setup`, `call`, `teardown`) to that list. Clearing during the call phase therefore wipes `get_records("setup")`, and all phases end up sharing one list. Gold adds `handler.clear()`, which clears the current list in place (`self.records.clear()`), so the stash references stay valid and are not merged.

**Evidence.** `test_clear_for_call_stage`: after `caplog.clear()`, `assert [x.message for x in caplog.get_records("setup")] == ["a_setup_log"]` fails with `AssertionError: assert [] == ['a_setup_log']`.

### final-swe-scikit-learn-scikit-learn-14983-fail: `broke_other_code`

**Reason.** The patch replaces the shared `_build_repr` `__repr__` on `BaseCrossValidator` and `BaseShuffleSplit` with a hard-coded format that reads `self.n_splits`, `self.n_repeats` and `self.random_state`. This causes two failures:
- `repr()` raises `AttributeError` for the many subclasses that lack those attributes.
- `RepeatedKFold` and `RepeatedStratifiedKFold` inherit from `_RepeatedSplits`, which the patch does not touch, so they still print the default object repr.

Gold adds `__repr__` to `_RepeatedSplits` and teaches `_build_repr` to read values from `cvargs`.

**Evidence.**
- `'LeaveOneOut' object has no attribute 'n_splits'`
- `'GroupShuffleSplit' object has no attribute 'n_repeats'`
- `'LeaveOneGroupOut' object has no attribute 'n_splits'`
- `assert 'RepeatedKFold(n_repeats=6, n_splits=2, random_state=None)' == '<sklearn.model_selection._split.RepeatedKFold object at 0x...>'`

### final-swe-sphinx-doc-sphinx-10323-fail: `did_not_fix`

**Reason.** The patch includes the correct fix: it moves `dedent_filter` before `prepend_filter` and `append_filter`, exactly as gold does. It also rewrites prepend and append to `lstrip()` the option text and copy the indentation of the first or last non-empty included line. A prepended outer-level line such as `class Foo:` is therefore indented to the body's level, and the same happens to the appended `# comment`. The extra auto-indent heuristic is what breaks the output.

**Evidence.** `test_LiteralIncludeReader_dedent_and_append_and_prepend`: `assert '  class Foo:...  # comment\n' == 'class Foo:\n...\n# comment\n'`.

### final-swe-sympy-sympy-12419-fail: `did_not_fix`

**Reason.** The root cause is `Identity._entry(i, j)`. For symbolic indices, `i == j` is structurally False, so it returns `S.Zero` and `Sum(Sum(e[i,j]))` evaluates to 0. The patch adds an `_eval_sum_elements` method that no summation code ever calls, and it leaves `_entry` unchanged. Gold returns `KroneckerDelta(i, j)` when `Eq(i, j)` is undecided.

**Evidence.** `test_Identity`, line 90: `assert In[i, j] != 0` fails with `AssertionError`.
