# Skill Manager

The skill manager describes, acquires, classifies, and activates filesystem-based AI agent skills from shared sources.

## Language

**Source**:
A repository or local location containing one or more skills. A source is the unit of version selection, locking, and acquisition.
_Avoid_: Repo, package

**Skill**:
A discovered directory containing a `SKILL.md`. A skill inherits classification from its source unless it declares refinements.
_Avoid_: Plugin, command

**Requested ref**:
The branch, tag, or commit named in a source declaration.
_Avoid_: Version, pin

**Resolved revision**:
The exact immutable source revision selected for a requested ref.
_Avoid_: Version, ref

**Tag**:
A label used to classify and select skills. A skill's effective tags combine inherited source tags with skill-specific refinements.
_Avoid_: Category

**Profile**:
A named set of selectors determining which skills are active.
_Avoid_: Pack, skillset

**Available skill**:
A skill acquired into canonical storage but not necessarily exposed to a consumer.
_Avoid_: Installed skill

**Active skill**:
An available skill exposed to a consumer by the selected profile.
_Avoid_: Loaded skill, installed skill

**Consumer directory**:
A filesystem directory scanned by an agent runtime for active skills.
_Avoid_: Install directory, target

**Reserved skill name**:
A name within a shared consumer directory whose entry is owned by another manager. The skill manager never creates, changes, or removes that entry.
_Avoid_: Ignored skill, excluded skill

**Activation scope**:
The filesystem and process boundary within which a profile is exposed to agent harnesses. Project scope is the default; global scope requires explicit opt-in.
_Avoid_: Install scope

**Source store**:
Persistent manager-owned storage for acquired revisions and generated profile views. Harnesses do not discover skills directly from this location.
_Avoid_: Global skills directory

**Project binding**:
A machine-local association between a project and an activation profile, normally stored in the clone's local Git configuration. A committed project declaration is an explicit sharing mechanism rather than the default.
_Avoid_: Project config file

**Project refinement**:
A clone-local skill or tag inclusion or exclusion layered over a project's base profile. Explicit skill refinements take precedence over tag refinements, and exclusions take precedence over inclusions at the same specificity.
_Avoid_: Project dependency, profile mutation
