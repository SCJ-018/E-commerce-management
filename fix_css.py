import re

with open(r'D:\代码\电商后台管理\index.html', 'r', encoding='utf-8') as f:
    content = f.read()

# Find the SECOND .placeholder-card block (old duplicate)
positions = [i for i in range(len(content)) if content.startswith('.placeholder-card {', i)]
print(f'Found {len(positions)} occurrences of .placeholder-card')

if len(positions) > 1:
    idx = positions[1]
    # Find the closing </style> that ends the OLD block
    closing_style = content.index('    </style>', idx)
    # Find the sidebar HTML that follows
    sidebar_html = content.index('    <!-- 侧边栏 -->', closing_style)
    
    # Build replacement: close first style block, then sidebar
    replacement = '''
    </style>

    <div id="sidebarOverlay" class="overlay hidden" onclick="App.toggleSidebar('close')"></div>

    <!-- 侧边栏 -->'''
    
    old_block = content[idx:sidebar_html]
    content = content.replace(old_block, replacement)
    
    with open(r'D:\代码\电商后台管理\index.html', 'w', encoding='utf-8') as f:
        f.write(content)
    print('Successfully removed duplicate CSS block')
else:.
    print('Only one .placeholder-card found, checking alternatives...')
    # Maybe the old block starts differently now
    # Look for the closing of the new style block
    closing_positions = [i for i in range(len(content)) if content.startswith('    </style>', i)]
    print(f'Found {len(closing_positions)} closing </style> tags')
    for i, pos in enumerate(closing_positions):
        context = content[max(0,pos-30):pos+30].replace('\n','\\n')
        print(f'  Closing {i}: ...{context}...')
