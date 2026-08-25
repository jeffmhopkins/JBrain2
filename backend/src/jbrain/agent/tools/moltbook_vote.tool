---
name: moltbook_vote
version: 1
permission: web
params:
  type: object
  properties:
    target_id:
      type: string
      description: The post or comment id to vote on.
    up:
      type: boolean
      description: true to upvote (default), false to downvote a post.
    comment:
      type: boolean
      description: true if target_id is a comment (comments can only be upvoted).
  required: [target_id]
---
STAGE a vote on a post or comment. A small way to say "this was worth it." Stages and
applies when released.
