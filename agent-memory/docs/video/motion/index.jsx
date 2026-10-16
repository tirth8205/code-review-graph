import React from 'react';
import {Composition, registerRoot} from 'remotion';
import {AgentMemoryFilm} from './composition.jsx';

const Root = () => <Composition
  id="AgentMemory4K"
  component={AgentMemoryFilm}
  durationInFrames={1440}
  fps={24}
  width={3840}
  height={2160}
  defaultProps={{captions: []}}
/>;

registerRoot(Root);
