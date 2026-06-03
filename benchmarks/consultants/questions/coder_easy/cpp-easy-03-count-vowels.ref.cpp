#include <iostream>
#include <cctype>
int main() {
    char ch; int n = 0;
    while (std::cin.get(ch)) {
        char l = (char)std::tolower((unsigned char)ch);
        if (l=='a'||l=='e'||l=='i'||l=='o'||l=='u') n++;
    }
    std::cout << n << std::endl;
    return 0;
}
