from setuptools import setup, find_packages

setup(
    name='django-chat-ui-backend',
    version='0.1.0',
    packages=find_packages(include=['chatbot', 'chatbot.*']),
    include_package_data=True,
    install_requires=[
        'Django>=3.2',
        'requests>=2.25.1',
        'google-genai>=0.1.0',
    ],
    description='A reusable Django app for a chat UI with GCP/Gemini integrations and Tailwind CSS.',
    long_description=open('README.md').read() if open('README.md') else 'A reusable Django app for chat UI.',
    long_description_content_type='text/markdown',
    url='https://github.com/G45P4R82/django-ui-chat',
    author='G45P4R82',
    classifiers=[
        'Environment :: Web Environment',
        'Framework :: Django',
        'Intended Audience :: Developers',
        'Operating System :: OS Independent',
        'Programming Language :: Python',
        'Programming Language :: Python :: 3',
    ],
)
