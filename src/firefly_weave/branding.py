# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0

"""Lumi artwork for self-contained workflow drawings and the API explorer."""

# One mascot per SVG: its gradient IDs are namespaced to avoid graph marker IDs.
LUMI_SVG_BODY = """<defs><radialGradient id="lumi-face" cx=".3" cy=".2" r=".9">
    <stop stop-color="#3C6957"/><stop offset=".55" stop-color="#1C463A"/>
    <stop offset="1" stop-color="#102E28"/>
  </radialGradient>
  <linearGradient id="lumi-eye" x1="0" y1="0" x2="0" y2="1">
    <stop stop-color="#FFFFFF"/><stop offset="1" stop-color="#B8DCC8"/>
  </linearGradient>
  <linearGradient id="lumi-lantern" x1="0" y1="0" x2="0" y2="1">
    <stop stop-color="#FFEDAD"/><stop offset="1" stop-color="#DDA743"/>
  </linearGradient>
</defs>
<g stroke-linecap="round" stroke-linejoin="round">
  <path d="M113 89C109 67 100 51 89 45M143 89C147 67 156 51 167 45" fill="none" stroke="#315C4E" stroke-width="3.5"/>
  <path d="M102 134Q128 122 154 134L153 184C153 206 142 219 128 219C114 219 103 206 103 184Z" fill="#173D34"/>
  <path d="M107 177Q128 185 149 177L148 191C146 205 138 213 128 213C118 213 110 205 108 191Z"
    fill="url(#lumi-lantern)"/>
  <path d="M30 83L56 61Q61 57 66 64L142 174L130 193L28 91Q24 86 30 83Z" fill="#173D34"/>
  <path d="M57 63L135 175L129 185L30 87Z" fill="#78B89C"/>
  <path d="M57 63L96 119L72 111L30 87Z" fill="#C7E6D7"/>
  <path d="M72 111L96 119L135 175L129 185Z" fill="#4F9479"/>
  <path d="M226 83L200 61Q195 57 190 64L111 178L127 194L228 91Q232 86 226 83Z" fill="#173D34"/>
  <path d="M199 63L116 178L127 187L226 87Z" fill="#A1D1B9"/>
  <path d="M199 63L160 119L184 111L226 87Z" fill="#D8EEE3"/>
  <path d="M184 111L160 119L116 178L127 187Z" fill="#68AA8D"/>
  <g transform="rotate(-5 128 120)">
    <path d="M94 115C93 95 108 81 128 81C148 81 163 95 162 115
      C162 135 150 152 130 158C109 155 95 138 94 115Z" fill="url(#lumi-face)"/>
    <path d="M98 111C98 96 110 86 124 85" fill="none" stroke="#83AF94"
      stroke-width="1.2" opacity=".5"/>
    <path d="M128 87C124 99 124 106 126 112" fill="none" stroke="#58856B"
      stroke-width="1" opacity=".4"/>
    <path d="M103 114C104 106 111 103 117 107C124 111 124 120 119 126
      C111 131 104 124 103 114Z" fill="#0C2923"/>
    <path d="M105 114C106 109 111 107 116 110C120 113 121 120 117 124
      C111 127 106 122 105 114Z" fill="url(#lumi-eye)"/>
    <ellipse cx="114.5" cy="116.5" rx="3.3" ry="5.4" fill="#173D34"/>
    <circle cx="115.5" cy="113.5" r="1.4" fill="#FFFFFF"/>
    <path d="M135 112C138 105 145 103 151 108C156 114 153 124 147 127
      C139 129 133 120 135 112Z" fill="#0C2923"/>
    <path d="M137 113C139 108 144 107 148 110C152 114 150 121 146 124
      C140 126 136 120 137 113Z" fill="url(#lumi-eye)"/>
    <ellipse cx="145" cy="116" rx="3.3" ry="5.4" fill="#173D34"/>
    <circle cx="146" cy="113" r="1.4" fill="#FFFFFF"/>
    <path d="M106 101Q112 98 118 102M137 101Q143 98 149 102"
      fill="none" stroke="#6C997E" stroke-width="1.8" opacity=".65"/>
    <path d="M122 140Q129 143 136 139" fill="none" stroke="#84B295" stroke-width="1.5"/>
  </g>
</g>"""
